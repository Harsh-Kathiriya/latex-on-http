# -*- coding: utf-8 -*-
"""Focused tests for sanitized persistent LaTeX compile state."""

import os
import threading

import pytest

from latexonhttp.workspaces import compile_cache


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_DIRECTORY", str(cache_dir))
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_ENABLED", True)
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_MAX_ENTRIES", 1000)
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_MAX_ENTRY_FILES", 256)
    monkeypatch.setattr(
        compile_cache, "COMPILE_CACHE_MAX_ENTRY_BYTES", 32 * 1024 * 1024
    )
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_MAX_SIZE_MB", 512)
    with compile_cache._cache_locks_guard:
        compile_cache._cache_locks.clear()
    yield cache_dir
    with compile_cache._cache_locks_guard:
        compile_cache._cache_locks.clear()


def _resource(content, path="__main_document__.tex", *, main=False):
    return {
        "type": "utf8/string",
        "build_path": path,
        "body_source": {"raw_string": content},
        "is_main_document": main,
    }


def _key(seed="document"):
    return compile_cache.compute_compile_key([_resource(seed)], "pdflatex")


def _write_entry(cache_dir, key, last_used, size=1):
    entry = cache_dir / key
    entry.mkdir()
    (entry / "document.aux").write_bytes(b"x" * size)
    assert compile_cache._write_last_used(str(entry), last_used)
    return entry


def test_scoped_key_is_stable_across_edits_and_isolated_by_scope():
    scope_a = "a" * 64
    scope_b = "b" * 64

    first = compile_cache.compute_compile_key(
        [_resource("first draft")], "pdflatex", cache_scope=scope_a
    )
    edited = compile_cache.compute_compile_key(
        [_resource("second draft")], "pdflatex", cache_scope=scope_a
    )
    other_scope = compile_cache.compute_compile_key(
        [_resource("first draft")], "pdflatex", cache_scope=scope_b
    )

    assert first == edited
    assert first != other_scope
    assert compile_cache.validate_cache_scope(scope_a)
    assert first.startswith(f"{compile_cache.CACHE_KEY_VERSION}-")
    assert len(first.removeprefix(f"{compile_cache.CACHE_KEY_VERSION}-")) == 64


def test_scoped_key_includes_paths_compiler_and_compile_options():
    scope = "a" * 64
    base_resources = [_resource("draft")]
    base = compile_cache.compute_compile_key(
        base_resources, "pdflatex", cache_scope=scope
    )

    assert base != compile_cache.compute_compile_key(
        base_resources + [_resource("citation", "references.bib")],
        "pdflatex",
        cache_scope=scope,
    )
    assert base != compile_cache.compute_compile_key(
        base_resources, "xelatex", cache_scope=scope
    )
    assert base != compile_cache.compute_compile_key(
        base_resources,
        "pdflatex",
        options={"bibliography": {"command": "biber"}},
        cache_scope=scope,
    )
    assert base != compile_cache.compute_compile_key(
        [_resource("draft", main=True)], "pdflatex", cache_scope=scope
    )


def test_unscoped_key_uses_content_and_is_order_independent():
    first = _resource("alpha")
    second = _resource("beta", "chapter.tex")

    key = compile_cache.compute_compile_key([first, second], "pdflatex")

    assert key == compile_cache.compute_compile_key([second, first], "pdflatex")
    assert key != compile_cache.compute_compile_key(
        [_resource("changed"), second], "pdflatex"
    )


@pytest.mark.parametrize(
    "scope",
    [None, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, 123],
)
def test_validate_cache_scope_rejects_malformed_values(scope):
    assert not compile_cache.validate_cache_scope(scope)


def test_compute_key_rejects_malformed_non_null_scope():
    with pytest.raises(ValueError, match="64 lowercase hex"):
        compile_cache.compute_compile_key(
            [_resource("draft")], "pdflatex", cache_scope="client-project-id"
        )


def test_publish_and_restore_only_allowlisted_auxiliary_files(isolated_cache, tmp_path):
    workspace = tmp_path / "workspace"
    nested = workspace / "chapters"
    nested.mkdir(parents=True)

    expected = {
        "document.aux": b"auxiliary",
        "document.fdb_latexmk": b"latexmk-state",
        "document.toc": b"table-of-contents",
        "document.bbl": b"bibliography",
        "chapters/chapter.aux": b"nested-auxiliary",
    }
    for relative_path, content in expected.items():
        path = workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    submitted_aux = workspace / "submitted.aux"
    submitted_aux.write_bytes(b"user-supplied-aux")
    forbidden = {
        "document.tex": b"source",
        "figure.png": b"image",
        "document.pdf": b"pdf",
        "document.log": b"log",
        "document.blg": b"bibliography-log",
        "document.synctex.gz": b"synctex",
        ".latexmkrc": b"config",
    }
    for relative_path, content in forbidden.items():
        (workspace / relative_path).write_bytes(content)

    outside = tmp_path / "outside-secret.aux"
    outside.write_bytes(b"must-not-follow")
    os.symlink(outside, workspace / "linked.aux")

    key = _key()
    assert compile_cache.publish_compile_state(
        key,
        str(workspace),
        ["document.tex", "figure.png", "submitted.aux"],
    )

    entry = isolated_cache / key
    cached_files = {
        path.relative_to(entry).as_posix()
        for path in entry.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert cached_files == set(expected) | {".last_used"}

    restored = tmp_path / "restored"
    restored.mkdir()
    assert compile_cache.restore_compile_state(key, str(restored))
    for relative_path, content in expected.items():
        assert (restored / relative_path).read_bytes() == content
    for relative_path in set(forbidden) | {"submitted.aux", "linked.aux"}:
        assert not (restored / relative_path).exists()


def test_restore_does_not_follow_a_cache_symlink(isolated_cache, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key = _key()
    entry = _write_entry(isolated_cache, key, time_value := 100_000.0)
    outside = tmp_path / "outside.aux"
    outside.write_bytes(b"outside")
    os.symlink(outside, entry / "linked.aux")

    monkeypatch.setattr(compile_cache.time, "time", lambda: time_value + 1)
    assert compile_cache.restore_compile_state(key, str(workspace))

    assert (workspace / "document.aux").read_bytes() == b"x"
    assert not (workspace / "linked.aux").exists()


def test_cache_expires_at_exactly_twenty_four_hours(
    isolated_cache, tmp_path, monkeypatch
):
    now = 200_000.0
    key = _key()
    entry = _write_entry(
        isolated_cache,
        key,
        now - compile_cache.COMPILE_CACHE_MAX_AGE_SECONDS,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(compile_cache.time, "time", lambda: now)

    assert compile_cache.COMPILE_CACHE_MAX_AGE_SECONDS == 86400
    assert not compile_cache.restore_compile_state(key, str(workspace))
    assert not entry.exists()


def test_restore_failure_removes_partial_files_and_created_directories(
    isolated_cache, tmp_path, monkeypatch
):
    key = _key()
    entry = isolated_cache / key
    (entry / "a").mkdir(parents=True)
    (entry / "a" / "first.aux").write_bytes(b"first")
    (entry / "b").mkdir()
    (entry / "b" / "second.aux").write_bytes(b"second")
    assert compile_cache._write_last_used(str(entry))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    original_copy = compile_cache._copy_regular_file
    copy_count = 0

    def fail_second_copy(source, destination, source_stat):
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
            raise OSError("simulated cache read failure")
        original_copy(source, destination, source_stat)

    monkeypatch.setattr(compile_cache, "_copy_regular_file", fail_second_copy)

    assert not compile_cache.restore_compile_state(key, str(workspace))
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("submitted_paths", ["document.aux", ["../document.aux"]])
def test_publish_rejects_unsafe_submitted_path_shapes(
    isolated_cache, tmp_path, submitted_paths
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "document.aux").write_bytes(b"auxiliary")
    key = _key()

    assert not compile_cache.publish_compile_state(key, str(workspace), submitted_paths)
    assert not (isolated_cache / key).exists()


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("COMPILE_CACHE_MAX_ENTRY_FILES", 1),
        ("COMPILE_CACHE_MAX_ENTRY_BYTES", 3),
    ],
)
def test_publish_enforces_per_entry_limits(
    isolated_cache, tmp_path, monkeypatch, limit_name, limit_value
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "first.aux").write_bytes(b"aa")
    (workspace / "second.aux").write_bytes(b"bb")
    key = _key()
    monkeypatch.setattr(compile_cache, limit_name, limit_value)

    assert not compile_cache.publish_compile_state(key, str(workspace), [])
    assert not (isolated_cache / key).exists()


def test_publish_flushes_cache_root_after_atomic_rename(
    isolated_cache, tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "document.aux").write_bytes(b"auxiliary")
    flushed = []
    monkeypatch.setattr(
        compile_cache, "_fsync_directory", lambda path: flushed.append(path)
    )

    assert compile_cache.publish_compile_state(_key(), str(workspace), [])
    assert flushed == [str(isolated_cache)]


def test_cache_lock_is_nonblocking_and_can_be_reacquired(isolated_cache):
    key = _key()
    assert compile_cache.acquire_cache_lock(key)

    result = []
    contender = threading.Thread(
        target=lambda: result.append(compile_cache.acquire_cache_lock(key))
    )
    contender.start()
    contender.join(timeout=2)

    assert result == [False]
    compile_cache.release_cache_lock(key)
    assert compile_cache.acquire_cache_lock(key)
    compile_cache.release_cache_lock(key)


def test_eviction_removes_expired_and_oldest_excess_entries(
    isolated_cache, monkeypatch
):
    now = 300_000.0
    expired_key = _key("expired")
    oldest_key = _key("oldest")
    newest_key = _key("newest")
    expired = _write_entry(
        isolated_cache,
        expired_key,
        now - compile_cache.COMPILE_CACHE_MAX_AGE_SECONDS,
    )
    oldest = _write_entry(isolated_cache, oldest_key, now - 100)
    newest = _write_entry(isolated_cache, newest_key, now - 10)
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_MAX_ENTRIES", 1)
    monkeypatch.setattr(compile_cache.time, "time", lambda: now)

    compile_cache._run_eviction_sync()

    assert not expired.exists()
    assert not oldest.exists()
    assert newest.exists()


def test_eviction_enforces_total_size_and_skips_locked_entry(
    isolated_cache, monkeypatch
):
    now = 400_000.0
    locked_key = _key("locked")
    newer_key = _key("newer")
    locked = _write_entry(isolated_cache, locked_key, now - 100, size=700_000)
    newer = _write_entry(isolated_cache, newer_key, now - 10, size=700_000)
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_MAX_SIZE_MB", 1)
    monkeypatch.setattr(compile_cache.time, "time", lambda: now)

    assert compile_cache.acquire_cache_lock(locked_key)
    try:
        compile_cache._run_eviction_sync()
    finally:
        compile_cache.release_cache_lock(locked_key)

    assert locked.exists()
    assert newer.exists()

    compile_cache._run_eviction_sync()
    assert not locked.exists()
    assert newer.exists()


def test_eviction_keeps_an_entry_refreshed_after_the_scan(isolated_cache, monkeypatch):
    now = 450_000.0
    key = _key("refreshed")
    entry = _write_entry(
        isolated_cache,
        key,
        now - compile_cache.COMPILE_CACHE_MAX_AGE_SECONDS,
    )
    monkeypatch.setattr(compile_cache.time, "time", lambda: now)
    original_acquire = compile_cache.acquire_cache_lock
    refreshed = False

    def refresh_before_lock(candidate):
        nonlocal refreshed
        if candidate == key and not refreshed:
            refreshed = True
            assert compile_cache._write_last_used(str(entry), now)
        return original_acquire(candidate)

    monkeypatch.setattr(compile_cache, "acquire_cache_lock", refresh_before_lock)

    compile_cache._run_eviction_sync()

    assert entry.exists()


def test_eviction_unlinks_cache_entry_symlink_without_following_it(
    isolated_cache, tmp_path
):
    key = _key("symlink")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "document.aux").write_bytes(b"outside")
    assert compile_cache._write_last_used(str(outside), 0)
    link = isolated_cache / key
    os.symlink(outside, link)

    compile_cache._run_eviction_sync()

    assert not link.exists()
    assert (outside / "document.aux").read_bytes() == b"outside"
