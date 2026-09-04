# -*- coding: utf-8 -*-
"""
latexonhttp.workspaces.compile_cache
~~~~~~~~~~~~~~~~~~~~~
Persistent compile directory cache for reusing LaTeX auxiliary files
(.aux, .fdb_latexmk, .toc, .bbl, etc.) across compilations.

When the same set of files is compiled repeatedly, latexmk can skip
redundant passes by inspecting preserved auxiliary files from the
prior compile. This mirrors Overleaf's CLSI approach of keeping a
persistent compile directory per project.

:copyright: (c) 2025 LaTeX-On-HTTP contributors.
:license: AGPL, see LICENSE for more details.
"""
import hashlib
import logging
import os
import os.path
import shutil
import threading
import time

logger = logging.getLogger(__name__)

COMPILE_CACHE_ENABLED = os.getenv("COMPILE_CACHE_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
COMPILE_CACHE_DIRECTORY = os.getenv(
    "COMPILE_CACHE_DIRECTORY", "/mnt/compile-cache"
)
COMPILE_CACHE_MAX_ENTRIES = int(os.getenv("COMPILE_CACHE_MAX_ENTRIES", "1000"))
COMPILE_CACHE_MAX_AGE_SECONDS = int(
    os.getenv("COMPILE_CACHE_MAX_AGE_SECONDS", "2592000")
)
COMPILE_CACHE_MAX_SIZE_MB = int(os.getenv("COMPILE_CACHE_MAX_SIZE_MB", "512"))

_EVICTION_INTERVAL_SECONDS = 60

# Thread-safe locking: one lock per compile key prevents concurrent
# compilations from corrupting the same directory. A meta-lock
# protects the dict of per-key locks itself.
_compile_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()

_LOCK_FILENAME = ".compile_lock"
_LOCK_STALE_SECONDS = 300

# Track last-used timestamps in-memory for LRU eviction.
_last_used: dict[str, float] = {}
_last_eviction_time: float = 0.0


_PROJECT_PREFIX = ".project/"


def compute_compile_key(
    normalized_resources: list[dict],
    compiler_name: str,
    options: dict | None = None,
) -> str:
    """Derive a stable cache key from resource identity and compiler.

    When the client supplies a project marker resource (a resource whose
    ``build_path`` starts with ``.project/``), the embedded project ID
    makes the key unique per project while keeping it **stable across
    edits**.  Inline content hashes are omitted so that ``latexmk`` can
    reuse auxiliary files from the previous compile in the same
    directory, cutting redundant passes from ~3 to 1.

    Without a project marker the key falls back to content-based
    hashing, which is safe for anonymous / one-off API callers but
    produces a new directory on every edit.
    """
    project_id = _extract_project_id(normalized_resources)
    has_project = project_id is not None

    parts = [compiler_name]
    if project_id:
        parts.append(f"project={project_id}")
    if options:
        bib_cmd = str(options.get("bibliography", {}).get("command", ""))
        halt = str(options.get("compiler", {}).get("halt_on_error", ""))
        silent = str(options.get("compiler", {}).get("silent", ""))
        parts.append(f"bib={bib_cmd}")
        parts.append(f"halt={halt}")
        parts.append(f"silent={silent}")
    for resource in sorted(
        normalized_resources, key=lambda r: r.get("build_path") or ""
    ):
        build_path = resource.get("build_path") or ""
        if build_path.startswith(_PROJECT_PREFIX):
            continue
        if build_path:
            parts.append(build_path)
        if has_project:
            continue
        body_source = resource.get("body_source") or {}
        url = body_source.get("url")
        cache_hash = body_source.get("hash")
        raw_string = body_source.get("raw_string")
        raw_base64 = body_source.get("raw_base64")
        if url:
            parts.append(url)
        elif cache_hash:
            parts.append(f"cache={cache_hash}")
        elif raw_string is not None:
            content_hash = hashlib.sha256(raw_string.encode("utf-8")).hexdigest()[:12]
            parts.append(f"inline={content_hash}")
        elif raw_base64 is not None:
            content_hash = hashlib.sha256(raw_base64.encode("utf-8")).hexdigest()[:12]
            parts.append(f"b64={content_hash}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _extract_project_id(normalized_resources: list[dict]) -> str | None:
    """Return the project ID from a ``.project/<id>`` resource, or None."""
    for resource in normalized_resources:
        build_path = resource.get("build_path") or ""
        if build_path.startswith(_PROJECT_PREFIX):
            return build_path[len(_PROJECT_PREFIX):]
    return None


def _get_compile_dir(compile_key: str) -> str:
    return os.path.abspath(os.path.join(COMPILE_CACHE_DIRECTORY, compile_key))


def _lock_file_path(compile_dir: str) -> str:
    return os.path.join(compile_dir, _LOCK_FILENAME)


def _try_acquire_file_lock(compile_dir: str) -> bool:
    """Create a lock file to signal cross-instance ownership.

    Uses O_CREAT|O_EXCL for atomic creation.  If the file already
    exists, checks whether it is stale (older than _LOCK_STALE_SECONDS)
    and reclaims it — this handles the case where a container crashed
    without releasing.
    """
    lock_path = _lock_file_path(compile_dir)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except OSError:
            return False
        if age > _LOCK_STALE_SECONDS:
            logger.info("Reclaiming stale file lock: %s (age=%.0fs)", lock_path, age)
            try:
                os.remove(lock_path)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except (OSError, FileExistsError):
                return False
        return False
    except OSError:
        return False


def _release_file_lock(compile_dir: str) -> None:
    """Remove the lock file so other instances can use this directory."""
    try:
        os.remove(_lock_file_path(compile_dir))
    except OSError:
        pass


def _is_file_locked(compile_dir: str) -> bool:
    """Check whether another instance holds the file lock."""
    lock_path = _lock_file_path(compile_dir)
    if not os.path.exists(lock_path):
        return False
    try:
        age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        return False
    return age <= _LOCK_STALE_SECONDS


def _dir_size_bytes(path: str) -> int:
    """Total size of all files in a directory tree."""
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def acquire_compile_dir(compile_key: str) -> tuple[bool, str | None]:
    """Try to acquire exclusive access to a persistent compile directory.

    Returns (True, dir_path) if the lock was acquired, or (False, None)
    if another compilation already holds this key. The caller must call
    release_compile_dir() when done.

    Two layers of locking protect the directory:
      1. In-process threading.Lock — prevents races within this Gunicorn
         worker (multiple threads serving concurrent requests).
      2. Filesystem lock file (.compile_lock) — prevents races across
         container instances sharing the same R2-backed mount.
    """
    if not COMPILE_CACHE_ENABLED:
        return False, None

    with _meta_lock:
        if compile_key not in _compile_locks:
            _compile_locks[compile_key] = threading.Lock()
        lock = _compile_locks[compile_key]

    acquired = lock.acquire(blocking=False)
    if not acquired:
        logger.info(
            "Compile cache key %s is thread-locked, falling back to ephemeral workspace",
            compile_key,
        )
        return False, None

    compile_dir = _get_compile_dir(compile_key)
    os.makedirs(compile_dir, exist_ok=True)

    if not _try_acquire_file_lock(compile_dir):
        lock.release()
        logger.info(
            "Compile cache key %s is file-locked by another instance, "
            "falling back to ephemeral workspace",
            compile_key,
        )
        return False, None

    _last_used[compile_key] = time.time()
    try:
        os.utime(compile_dir, None)
    except OSError:
        pass

    logger.info("Acquired persistent compile dir: %s", compile_dir)
    return True, compile_dir


def release_compile_dir(compile_key: str) -> None:
    """Release the lock on a compile directory (keeping its files for reuse)."""
    _release_file_lock(_get_compile_dir(compile_key))
    with _meta_lock:
        lock = _compile_locks.get(compile_key)
    if lock is not None:
        try:
            lock.release()
        except RuntimeError:
            pass


def invalidate_compile_dir(compile_key: str) -> None:
    """Delete a persistent compile directory (e.g. after a failed compile).

    Corrupted auxiliary files can cause infinite error loops, so any
    compile failure should wipe the cached state entirely.

    Safe for shared storage: only deletes if no other instance holds
    the file lock.  The caller is expected to already hold the lock
    (via acquire_compile_dir), so this releases it first, then deletes.
    """
    compile_dir = _get_compile_dir(compile_key)
    _release_file_lock(compile_dir)
    if os.path.isdir(compile_dir):
        if _is_file_locked(compile_dir):
            logger.warning(
                "Skipping invalidation of %s — locked by another instance",
                compile_dir,
            )
        else:
            logger.info("Invalidating compile cache dir: %s", compile_dir)
            shutil.rmtree(compile_dir, ignore_errors=True)
    _last_used.pop(compile_key, None)


def clean_stale_outputs(compile_dir: str, main_build_path: str) -> None:
    """Remove the specific compiler output files from a prior run.

    Only deletes files derived from the main document name (e.g.
    __main_document__.pdf, .log, .synctex.gz) in the compile root.
    User-supplied resources like logo.pdf or figure1.pdf are NOT
    touched — they will be overwritten by the fetch step if they
    changed, and must survive intact for \\includegraphics etc.
    """
    stem = main_build_path.rsplit(".", 1)[0] if "." in main_build_path else main_build_path
    stale_names = [
        f"{stem}.pdf",
        f"{stem}.log",
        f"{stem}.synctex.gz",
        f"{stem}.dvi",
        f"{stem}.xdv",
        f"{stem}.ps",
    ]
    for name in stale_names:
        path = os.path.join(compile_dir, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def persist_resource_to_compile_dir(
    compile_dir: str, resource: dict, data: bytes
) -> str | None:
    """Write a resource file into the persistent compile directory.

    Mirrors workspaces.filesystem.persist_resource_to_workspace but
    operates on an explicit directory path instead of a workspace_id.
    """
    build_path = resource.get("build_path")
    if not build_path:
        return "MISSING_BUILD_PATH"
    resource_full_path = os.path.abspath(os.path.join(compile_dir, build_path))
    # Trailing os.sep prevents sibling-directory prefix attacks:
    # without it, "/cache/abc" is a prefix of "/cache/abcdef/file".
    compile_dir_prefix = os.path.abspath(compile_dir) + os.sep
    if not resource_full_path.startswith(compile_dir_prefix):
        return "INVALID_PATH"
    os.makedirs(os.path.dirname(resource_full_path), exist_ok=True)
    with open(resource_full_path, "wb") as f:
        f.write(data)
    return None


def run_eviction() -> None:
    """Schedule eviction on a background daemon thread so the request
    path is never blocked by filesystem scanning.

    The actual work is throttled inside ``_run_eviction_sync`` to run
    at most once per ``_EVICTION_INTERVAL_SECONDS``.
    """
    if not COMPILE_CACHE_ENABLED:
        return
    now = time.time()
    if (now - _last_eviction_time) < _EVICTION_INTERVAL_SECONDS:
        return
    t = threading.Thread(target=_run_eviction_sync, daemon=True)
    t.start()


def _run_eviction_sync() -> None:
    """Remove old, excess, or over-size compile cache directories.

    Enforces three limits (checked in order):
      1. Max age — entries older than COMPILE_CACHE_MAX_AGE_SECONDS
      2. Max entry count — keep at most COMPILE_CACHE_MAX_ENTRIES
      3. Max total size — keep total cache under COMPILE_CACHE_MAX_SIZE_MB
    """
    global _last_eviction_time

    now = time.time()
    if (now - _last_eviction_time) < _EVICTION_INTERVAL_SECONDS:
        return
    _last_eviction_time = now

    if not os.path.isdir(COMPILE_CACHE_DIRECTORY):
        return

    entries: list[tuple[str, float, int]] = []  # (name, last_used, size_bytes)

    try:
        for name in os.listdir(COMPILE_CACHE_DIRECTORY):
            dir_path = os.path.join(COMPILE_CACHE_DIRECTORY, name)
            if not os.path.isdir(dir_path):
                continue
            last_used_ts = _last_used.get(name, 0)
            if last_used_ts == 0:
                try:
                    last_used_ts = os.path.getmtime(dir_path)
                except OSError:
                    last_used_ts = 0
            size = _dir_size_bytes(dir_path)
            entries.append((name, last_used_ts, size))
    except OSError:
        return

    to_evict: set[str] = set()
    remaining: list[tuple[str, float, int]] = []

    # 1) Evict entries older than max age
    for name, ts, size in entries:
        if (now - ts) > COMPILE_CACHE_MAX_AGE_SECONDS:
            to_evict.add(name)
        else:
            remaining.append((name, ts, size))

    # 2) Evict oldest entries if over max count
    if len(remaining) > COMPILE_CACHE_MAX_ENTRIES:
        remaining.sort(key=lambda x: x[1])
        overflow = len(remaining) - COMPILE_CACHE_MAX_ENTRIES
        for name, _, _ in remaining[:overflow]:
            to_evict.add(name)
        remaining = remaining[overflow:]

    # 3) Evict oldest entries if total size exceeds limit
    max_size_bytes = COMPILE_CACHE_MAX_SIZE_MB * 1024 * 1024
    total_size = sum(size for _, _, size in remaining)
    if total_size > max_size_bytes:
        remaining.sort(key=lambda x: x[1])
        for name, _, size in remaining:
            if total_size <= max_size_bytes:
                break
            to_evict.add(name)
            total_size -= size

    evicted_count = 0
    for name in to_evict:
        with _meta_lock:
            if name not in _compile_locks:
                _compile_locks[name] = threading.Lock()
            lock = _compile_locks[name]
        if not lock.acquire(blocking=False):
            continue
        try:
            dir_path = os.path.join(COMPILE_CACHE_DIRECTORY, name)
            if _is_file_locked(dir_path):
                logger.info("Skipping eviction of %s — file-locked by another instance", name)
                continue
            logger.info("Evicting compile cache entry: %s", name)
            shutil.rmtree(dir_path, ignore_errors=True)
            _last_used.pop(name, None)
            evicted_count += 1
        finally:
            lock.release()

    _cleanup_stale_locks()

    if evicted_count:
        logger.info("Evicted %d compile cache entries", evicted_count)


def _cleanup_stale_locks() -> None:
    """Remove lock objects for compile keys whose directories no longer exist.

    Prevents unbounded growth of the _compile_locks dict over time.
    """
    with _meta_lock:
        stale_keys = [
            key
            for key in _compile_locks
            if not _compile_locks[key].locked()
            and not os.path.isdir(_get_compile_dir(key))
        ]
        for key in stale_keys:
            del _compile_locks[key]
            _last_used.pop(key, None)
