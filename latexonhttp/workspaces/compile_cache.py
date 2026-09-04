# -*- coding: utf-8 -*-
"""Persistent, best-effort snapshots of useful LaTeX compile state.

Compilation always happens in a request-local workspace. This module copies a
small allowlist of auxiliary files to and from the configured cache directory;
it never exposes the cache directory as a live compilation workspace. Cache
failures are deliberately non-fatal because a clean LaTeX build is always a
valid fallback.
"""

import base64
import binascii
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import tempfile
import threading
import time

logger = logging.getLogger(__name__)


COMPILE_CACHE_ENABLED = os.getenv("COMPILE_CACHE_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
COMPILE_CACHE_DIRECTORY = os.getenv(
    "COMPILE_CACHE_DIRECTORY", "./tmp/loh_compile_cache"
)
COMPILE_CACHE_MAX_ENTRIES = max(0, int(os.getenv("COMPILE_CACHE_MAX_ENTRIES", "1000")))
COMPILE_CACHE_MAX_AGE_SECONDS = 86400
COMPILE_CACHE_MAX_ENTRY_FILES = max(
    0, int(os.getenv("COMPILE_CACHE_MAX_ENTRY_FILES", "256"))
)
COMPILE_CACHE_MAX_ENTRY_BYTES = max(
    0,
    int(os.getenv("COMPILE_CACHE_MAX_ENTRY_BYTES", str(32 * 1024 * 1024))),
)
COMPILE_CACHE_MAX_SIZE_MB = max(0, int(os.getenv("COMPILE_CACHE_MAX_SIZE_MB", "512")))

CACHE_KEY_VERSION = "v1"

_CACHE_SCOPE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CACHE_KEY_PATTERN = re.compile(rf"^{CACHE_KEY_VERSION}-[0-9a-f]{{64}}$")
_LAST_USED_FILENAME = ".last_used"
_TEMP_DIRECTORY_PREFIX = ".tmp-compile-cache-"
_TEMP_DIRECTORY_MAX_AGE_SECONDS = 3600
_EVICTION_INTERVAL_SECONDS = 60

# These files are enough for latexmk, bibliography, index, glossary, and common
# list/cross-reference workflows. Source files, rendered output, logs,
# SyncTeX, images, and .latexmkrc are intentionally absent.
_CACHEABLE_SUFFIXES = (
    ".aux",
    ".bbl",
    ".bcf",
    ".fdb_latexmk",
    ".fls",
    ".toc",
    ".lof",
    ".lot",
    ".out",
    ".nav",
    ".snm",
    ".vrb",
    ".idx",
    ".ind",
    ".glo",
    ".gls",
    ".glsdefs",
    ".ist",
    ".acn",
    ".acr",
    ".run.xml",
)


class _LockState:
    """A lock plus references held by active acquire/release operations."""

    __slots__ = ("lock", "references")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.references = 0


_cache_locks: dict[str, _LockState] = {}
_cache_locks_guard = threading.Lock()

_eviction_state_guard = threading.Lock()
_eviction_running = False
_last_eviction_started = 0.0


def validate_cache_scope(cache_scope) -> bool:
    """Return whether *cache_scope* is an opaque, server-derived SHA-256 hex."""

    return (
        isinstance(cache_scope, str)
        and _CACHE_SCOPE_PATTERN.fullmatch(cache_scope) is not None
    )


def _compile_options(options: dict | None) -> dict[str, str]:
    """Return only options that can change LaTeX's reusable build state."""

    options = options if isinstance(options, dict) else {}
    bibliography = options.get("bibliography")
    compiler = options.get("compiler")
    bibliography = bibliography if isinstance(bibliography, dict) else {}
    compiler = compiler if isinstance(compiler, dict) else {}
    return {
        "bibliography_command": str(bibliography.get("command", "bibtex")),
        "halt_on_error": str(compiler.get("halt_on_error", False)),
        "silent": str(compiler.get("silent", False)),
    }


def _source_digest(resource: dict) -> str:
    """Hash an unscoped resource's content or immutable source identity."""

    body_source = resource.get("body_source")
    body_source = body_source if isinstance(body_source, dict) else {}

    raw_string = body_source.get("raw_string")
    if raw_string is not None:
        data = str(raw_string).encode("utf-8")
        source_type = b"utf8"
    else:
        raw_base64 = body_source.get("raw_base64")
        if raw_base64 is not None:
            encoded = str(raw_base64).encode("ascii", errors="backslashreplace")
            try:
                data = base64.b64decode(encoded, validate=True)
                source_type = b"base64"
            except (ValueError, binascii.Error):
                # Input validation will reject malformed base64 later. Keeping
                # key derivation deterministic still lets the cache fail open.
                data = encoded
                source_type = b"invalid-base64"
        elif body_source.get("hash") is not None:
            data = str(body_source["hash"]).encode("utf-8")
            source_type = b"resource-cache-hash"
        elif body_source.get("url") is not None:
            # The bytes are not available until fetching. Hash the complete
            # source identity rather than putting the URL in the cache key.
            data = str(body_source["url"]).encode("utf-8")
            source_type = b"url"
        else:
            data = b""
            source_type = b"missing"

    return hashlib.sha256(source_type + b"\0" + data).hexdigest()


def compute_compile_key(
    normalized_resources: list[dict],
    compiler_name: str,
    options: dict | None = None,
    cache_scope: str | None = None,
) -> str:
    """Derive a versioned, full SHA-256 key for reusable compile state.

    A trusted cache scope keeps the key stable as document contents change.
    Resource paths remain part of the key so adding, removing, or renaming a
    file cannot expose stale workspace inputs. Without a scope, every
    resource's content/source digest is included for safe one-off use.
    """

    if cache_scope is not None and not validate_cache_scope(cache_scope):
        raise ValueError("cache_scope must be exactly 64 lowercase hex characters")

    layout = sorted(
        (
            {
                "path": str(resource.get("build_path") or ""),
                "main": bool(resource.get("is_main_document")),
            }
            for resource in normalized_resources
        ),
        key=lambda resource: (resource["path"], resource["main"]),
    )
    key_material: dict[str, object] = {
        "version": CACHE_KEY_VERSION,
        "compiler": str(compiler_name),
        "options": _compile_options(options),
        "layout": layout,
    }

    if cache_scope is not None:
        key_material["scope"] = cache_scope
    else:
        identities = [
            {
                "path": str(resource.get("build_path") or ""),
                "type": str(resource.get("type") or ""),
                "digest": _source_digest(resource),
            }
            for resource in normalized_resources
        ]
        key_material["resources"] = sorted(
            identities,
            key=lambda identity: (
                identity["path"],
                identity["type"],
                identity["digest"],
            ),
        )

    encoded = json.dumps(
        key_material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"{CACHE_KEY_VERSION}-{hashlib.sha256(encoded).hexdigest()}"


def _valid_cache_key(key) -> bool:
    return isinstance(key, str) and _CACHE_KEY_PATTERN.fullmatch(key) is not None


def _drop_lock_reference(key: str, state: _LockState) -> None:
    with _cache_locks_guard:
        current = _cache_locks.get(key)
        if current is not state:
            return
        state.references -= 1
        if state.references == 0 and not state.lock.locked():
            del _cache_locks[key]


def acquire_cache_lock(key: str) -> bool:
    """Try to acquire this process's nonblocking lock for *key*."""

    if not COMPILE_CACHE_ENABLED or not _valid_cache_key(key):
        return False

    with _cache_locks_guard:
        state = _cache_locks.setdefault(key, _LockState())
        state.references += 1

    if state.lock.acquire(blocking=False):
        return True

    _drop_lock_reference(key, state)
    return False


def release_cache_lock(key: str) -> None:
    """Release a lock previously returned by :func:`acquire_cache_lock`."""

    with _cache_locks_guard:
        state = _cache_locks.get(key)
    if state is None:
        return

    try:
        state.lock.release()
    except RuntimeError:
        return
    _drop_lock_reference(key, state)


def _cache_root() -> str:
    return os.path.realpath(COMPILE_CACHE_DIRECTORY)


def _cache_entry_dir(key: str) -> str:
    if not _valid_cache_key(key):
        raise ValueError("invalid compile cache key")
    return os.path.join(_cache_root(), key)


def _is_directory_without_symlink(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _normalize_relative_path(path) -> str | None:
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    normalized = os.path.normpath(path)
    if (
        normalized in ("", ".", "..")
        or os.path.isabs(normalized)
        or normalized.startswith(f"..{os.sep}")
    ):
        return None
    return normalized.replace(os.sep, "/")


def _is_cacheable_relative_path(relative_path: str) -> bool:
    lower_path = relative_path.lower()
    return lower_path != _LAST_USED_FILENAME and lower_path.endswith(
        _CACHEABLE_SUFFIXES
    )


def _iter_regular_files(root: str):
    """Yield safe regular files below *root* without following symlinks."""

    def raise_walk_error(error):
        raise error

    for directory, directory_names, file_names in os.walk(
        root, followlinks=False, onerror=raise_walk_error
    ):
        safe_directory_names = []
        for name in directory_names:
            path = os.path.join(directory, name)
            try:
                if stat.S_ISDIR(os.lstat(path).st_mode):
                    safe_directory_names.append(name)
            except FileNotFoundError:
                continue
            except OSError:
                raise
        directory_names[:] = safe_directory_names

        for name in file_names:
            path = os.path.join(directory, name)
            try:
                file_stat = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                raise
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            relative_path = os.path.relpath(path, root).replace(os.sep, "/")
            normalized = _normalize_relative_path(relative_path)
            if normalized is not None:
                yield normalized, path, file_stat


def _safe_destination(
    root: str,
    relative_path: str,
    created_directories: list[str] | None = None,
) -> str:
    """Create safe parent directories and return a contained destination."""

    normalized = _normalize_relative_path(relative_path)
    if normalized is None:
        raise ValueError("unsafe relative path")

    current = root
    parts = normalized.split("/")
    for part in parts[:-1]:
        current = os.path.join(current, part)
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            if created_directories is not None:
                created_directories.append(current)
        else:
            if not stat.S_ISDIR(mode):
                raise OSError(f"unsafe destination component: {current}")

    destination = os.path.join(root, *parts)
    root_real = os.path.realpath(root)
    parent_real = os.path.realpath(os.path.dirname(destination))
    if os.path.commonpath((root_real, parent_real)) != root_real:
        raise ValueError("destination escapes workspace")
    return destination


def _copy_regular_file(source: str, destination: str, source_stat) -> None:
    """Copy one regular file without following source or destination symlinks."""

    source_flags = os.O_RDONLY
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW

    destination_created = False
    source_fd = os.open(source, source_flags)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise OSError("cache source is not a regular file")
        destination_fd = os.open(destination, destination_flags, 0o600)
        destination_created = True
        try:
            if not stat.S_ISREG(os.fstat(destination_fd).st_mode):
                raise OSError("cache destination is not a regular file")
            with os.fdopen(source_fd, "rb", closefd=False) as source_file:
                with os.fdopen(destination_fd, "wb", closefd=False) as destination_file:
                    shutil.copyfileobj(source_file, destination_file)
                    destination_file.flush()
                    os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    except Exception:
        if destination_created:
            try:
                if stat.S_ISREG(os.lstat(destination).st_mode):
                    os.unlink(destination)
            except OSError:
                pass
        raise
    finally:
        os.close(source_fd)

    try:
        os.utime(
            destination,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            follow_symlinks=False,
        )
    except OSError:
        try:
            if stat.S_ISREG(os.lstat(destination).st_mode):
                os.unlink(destination)
        except OSError:
            pass
        raise


def _fsync_directory(path: str) -> None:
    """Wait for a cache directory's pending metadata operations to flush."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_last_used(entry_dir: str, timestamp: float | None = None) -> bool:
    timestamp = time.time() if timestamp is None else timestamp
    marker_path = os.path.join(entry_dir, _LAST_USED_FILENAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if os.path.lexists(marker_path) and not stat.S_ISREG(
            os.lstat(marker_path).st_mode
        ):
            return False
        marker_fd = os.open(marker_path, flags, 0o600)
        try:
            os.write(marker_fd, f"{timestamp:.6f}\n".encode("ascii"))
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
        return True
    except OSError as error:
        logger.warning("Could not update compile cache last-used marker: %s", error)
        return False


def _read_last_used(entry_dir: str) -> float | None:
    marker_path = os.path.join(entry_dir, _LAST_USED_FILENAME)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        marker_fd = os.open(marker_path, flags)
        try:
            if not stat.S_ISREG(os.fstat(marker_fd).st_mode):
                return None
            with os.fdopen(marker_fd, "rb", closefd=False) as marker_file:
                raw_timestamp = marker_file.read(128)
        finally:
            os.close(marker_fd)
        timestamp = float(raw_timestamp.strip())
        return timestamp if math.isfinite(timestamp) and timestamp >= 0 else None
    except (OSError, ValueError):
        return None


def _remove_cache_path(path: str) -> bool:
    """Remove a cache path without following a top-level symlink."""

    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return True
    except OSError:
        return False

    try:
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        return True
    except OSError as error:
        logger.warning("Could not remove compile cache path %s: %s", path, error)
        return False


def _cleanup_restored_files(paths: list[str], created_directories: list[str]) -> None:
    for path in reversed(paths):
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                os.unlink(path)
        except OSError:
            pass
    for directory in reversed(created_directories):
        try:
            os.rmdir(directory)
        except OSError:
            pass


def restore_compile_state(key: str, workspace_dir: str) -> bool:
    """Restore an unexpired auxiliary snapshot into a local workspace.

    Returns ``False`` for cache misses and all storage failures. Existing
    workspace files are never overwritten.
    """

    if (
        not COMPILE_CACHE_ENABLED
        or not _valid_cache_key(key)
        or not _is_directory_without_symlink(workspace_dir)
    ):
        return False

    entry_dir = _cache_entry_dir(key)
    if not _is_directory_without_symlink(entry_dir):
        return False

    last_used = _read_last_used(entry_dir)
    if last_used is None or (time.time() - last_used) >= COMPILE_CACHE_MAX_AGE_SECONDS:
        _remove_cache_path(entry_dir)
        return False

    restored_paths: list[str] = []
    created_directories: list[str] = []
    try:
        candidates = []
        candidate_bytes = 0
        for relative_path, source, source_stat in _iter_regular_files(entry_dir):
            if not _is_cacheable_relative_path(relative_path):
                continue
            candidates.append((relative_path, source, source_stat))
            candidate_bytes += source_stat.st_size
            if (
                len(candidates) > COMPILE_CACHE_MAX_ENTRY_FILES
                or candidate_bytes > COMPILE_CACHE_MAX_ENTRY_BYTES
            ):
                logger.warning("Compile cache entry %s exceeds entry limits", key)
                _remove_cache_path(entry_dir)
                return False

        for relative_path, source, source_stat in candidates:
            destination = _safe_destination(
                workspace_dir, relative_path, created_directories
            )
            if os.path.lexists(destination):
                continue
            _copy_regular_file(source, destination, source_stat)
            restored_paths.append(destination)
    except (OSError, ValueError) as error:
        logger.warning("Could not restore compile cache entry %s: %s", key, error)
        _cleanup_restored_files(restored_paths, created_directories)
        return False

    if not restored_paths:
        return False

    _write_last_used(entry_dir)
    logger.info("Restored %d compile cache files for %s", len(restored_paths), key)
    return True


def publish_compile_state(key: str, workspace_dir: str, submitted_paths) -> bool:
    """Publish a sanitized auxiliary snapshot from a successful local build."""

    if (
        not COMPILE_CACHE_ENABLED
        or not _valid_cache_key(key)
        or not _is_directory_without_symlink(workspace_dir)
    ):
        return False

    if isinstance(submitted_paths, (str, bytes)):
        return False

    normalized_submitted_paths: set[str] = set()
    try:
        for path in submitted_paths:
            normalized = _normalize_relative_path(path)
            if normalized is None:
                logger.warning("Refusing to publish cache with unsafe submitted path")
                return False
            normalized_submitted_paths.add(normalized)
    except TypeError:
        return False

    entry_dir = _cache_entry_dir(key)
    candidates = []
    candidate_bytes = 0
    try:
        for relative_path, source, source_stat in _iter_regular_files(workspace_dir):
            if (
                not _is_cacheable_relative_path(relative_path)
                or relative_path in normalized_submitted_paths
            ):
                continue
            candidates.append((relative_path, source, source_stat))
            candidate_bytes += source_stat.st_size
            if (
                len(candidates) > COMPILE_CACHE_MAX_ENTRY_FILES
                or candidate_bytes > COMPILE_CACHE_MAX_ENTRY_BYTES
            ):
                logger.warning("Compile state for %s exceeds entry limits", key)
                _remove_cache_path(entry_dir)
                return False
    except OSError as error:
        logger.warning("Could not inspect compile state for %s: %s", key, error)
        return False

    if not candidates:
        _remove_cache_path(entry_dir)
        return False

    temporary_dir = None
    try:
        cache_root = _cache_root()
        os.makedirs(cache_root, mode=0o700, exist_ok=True)
        temporary_dir = tempfile.mkdtemp(prefix=_TEMP_DIRECTORY_PREFIX, dir=cache_root)

        for relative_path, source, source_stat in candidates:
            destination = _safe_destination(temporary_dir, relative_path)
            _copy_regular_file(source, destination, source_stat)

        if not _write_last_used(temporary_dir):
            return False

        if os.path.lexists(entry_dir) and not _remove_cache_path(entry_dir):
            return False
        os.replace(temporary_dir, entry_dir)
        temporary_dir = None
        _fsync_directory(cache_root)
        logger.info("Published %d compile cache files for %s", len(candidates), key)
        return True
    except (OSError, ValueError) as error:
        logger.warning("Could not publish compile cache entry %s: %s", key, error)
        return False
    finally:
        if temporary_dir is not None:
            _remove_cache_path(temporary_dir)


def _directory_size_bytes(path: str) -> int:
    return sum(
        file_stat.st_size
        for _relative_path, _file_path, file_stat in _iter_regular_files(path)
    )


def _run_eviction_sync() -> None:
    """Apply idle-age, entry-count, and total-size bounds to snapshots."""

    if not COMPILE_CACHE_ENABLED:
        return

    cache_root = _cache_root()
    if not _is_directory_without_symlink(cache_root):
        return

    now = time.time()
    entries: list[tuple[str, float, int]] = []
    try:
        names = os.listdir(cache_root)
    except OSError as error:
        logger.warning("Could not list compile cache: %s", error)
        return

    for name in names:
        path = os.path.join(cache_root, name)
        if name.startswith(_TEMP_DIRECTORY_PREFIX):
            try:
                if (now - os.lstat(path).st_mtime) >= _TEMP_DIRECTORY_MAX_AGE_SECONDS:
                    _remove_cache_path(path)
            except OSError:
                pass
            continue
        if not _valid_cache_key(name):
            continue
        if not _is_directory_without_symlink(path):
            if acquire_cache_lock(name):
                try:
                    _remove_cache_path(path)
                finally:
                    release_cache_lock(name)
            continue

        last_used = _read_last_used(path)
        if last_used is None:
            last_used = 0.0
        try:
            size = _directory_size_bytes(path)
        except OSError:
            size = 0
        entries.append((name, last_used, size))

    to_evict: dict[str, float] = {}
    remaining: list[tuple[str, float, int]] = []
    for name, last_used, size in entries:
        if (now - last_used) >= COMPILE_CACHE_MAX_AGE_SECONDS:
            to_evict[name] = last_used
        else:
            remaining.append((name, last_used, size))

    if len(remaining) > COMPILE_CACHE_MAX_ENTRIES:
        remaining.sort(key=lambda entry: (entry[1], entry[0]))
        overflow = len(remaining) - COMPILE_CACHE_MAX_ENTRIES
        to_evict.update(
            (name, last_used) for name, last_used, _size in remaining[:overflow]
        )
        remaining = remaining[overflow:]

    max_size_bytes = COMPILE_CACHE_MAX_SIZE_MB * 1024 * 1024
    total_size = sum(size for _name, _last_used, size in remaining)
    if total_size > max_size_bytes:
        remaining.sort(key=lambda entry: (entry[1], entry[0]))
        for name, _last_used, size in remaining:
            if total_size <= max_size_bytes:
                break
            to_evict[name] = _last_used
            total_size -= size

    evicted_count = 0
    for key, scanned_last_used in sorted(to_evict.items()):
        if not acquire_cache_lock(key):
            continue
        try:
            entry_dir = _cache_entry_dir(key)
            current_last_used = _read_last_used(entry_dir)
            # A restore may refresh the entry after the scan but before this
            # lock. Keep that newly used snapshot for the next bounded pass.
            if current_last_used is not None and current_last_used > scanned_last_used:
                continue
            if _remove_cache_path(entry_dir):
                evicted_count += 1
        finally:
            release_cache_lock(key)

    if evicted_count:
        logger.info("Evicted %d compile cache entries", evicted_count)


def _eviction_worker() -> None:
    global _eviction_running
    try:
        _run_eviction_sync()
    finally:
        with _eviction_state_guard:
            _eviction_running = False


def run_eviction() -> None:
    """Schedule one throttled, nonblocking cache eviction pass."""

    global _eviction_running, _last_eviction_started

    if not COMPILE_CACHE_ENABLED:
        return

    now = time.monotonic()
    with _eviction_state_guard:
        if (
            _eviction_running
            or (now - _last_eviction_started) < _EVICTION_INTERVAL_SECONDS
        ):
            return
        _eviction_running = True
        _last_eviction_started = now

    try:
        threading.Thread(target=_eviction_worker, daemon=True).start()
    except RuntimeError:
        with _eviction_state_guard:
            _eviction_running = False
