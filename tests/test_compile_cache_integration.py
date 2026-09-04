from flask import Flask
from pathlib import Path
import pytest

from latexonhttp.api import builds
from latexonhttp.workspaces import compile_cache, filesystem


@pytest.fixture
def compile_client(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspaces"
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(filesystem, "WORKSPACE_DIRECTORY", str(workspace_root))
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_DIRECTORY", str(cache_root))
    monkeypatch.setattr(compile_cache, "COMPILE_CACHE_ENABLED", True)
    monkeypatch.setattr(builds, "run_eviction", lambda: None)
    with compile_cache._cache_locks_guard:
        compile_cache._cache_locks.clear()

    test_app = Flask(__name__)
    test_app.register_blueprint(builds.builds_app, url_prefix="/builds")
    yield test_app.test_client(), cache_root

    with compile_cache._cache_locks_guard:
        compile_cache._cache_locks.clear()


def successful_result():
    return {
        "status": "ok",
        "pdf": b"%PDF-test",
        "log_files": {},
        "output_path": "output.pdf",
        "duration": 0.01,
        "logs": "",
        "is_timeout": False,
        "return_codes": [0],
        "parsed_log": {},
    }


def request_body(content):
    return {
        "cache_scope": "a" * 64,
        "resources": [{"main": True, "content": content}],
        "options": {"response": {"format": "json"}},
    }


def test_document_edit_restores_auxiliary_state(compile_client, monkeypatch):
    client, cache_root = compile_client
    restored_before_compile = []

    def fake_compile(_compiler, directory, _main, _workspace_id, _options):
        aux_path = Path(directory, "__main_document__.aux")
        restored_before_compile.append(aux_path.exists())
        aux_path.write_bytes(b"known-good-state")
        return successful_result()

    monkeypatch.setattr(builds, "latexToPdf", fake_compile)

    assert client.post("/builds/sync", json=request_body("first")).status_code == 201
    assert client.post("/builds/sync", json=request_body("edited")).status_code == 201

    assert restored_before_compile == [False, True]
    cached_names = {path.name for path in cache_root.rglob("*") if path.is_file()}
    assert cached_names == {"__main_document__.aux", ".last_used"}


def test_failed_compile_does_not_replace_last_good_snapshot(
    compile_client, monkeypatch
):
    client, cache_root = compile_client

    def first_compile(_compiler, directory, _main, _workspace_id, _options):
        Path(directory, "__main_document__.aux").write_bytes(b"known-good-state")
        return successful_result()

    monkeypatch.setattr(builds, "latexToPdf", first_compile)
    assert client.post("/builds/sync", json=request_body("first")).status_code == 201

    def failed_compile(_compiler, directory, _main, _workspace_id, _options):
        Path(directory, "__main_document__.aux").write_bytes(b"failed-state")
        return {
            **successful_result(),
            "status": "ko",
            "pdf": None,
            "logs": "compile failed",
        }

    monkeypatch.setattr(builds, "latexToPdf", failed_compile)
    assert client.post("/builds/sync", json=request_body("broken")).status_code == 400

    cached_aux = next(cache_root.rglob("__main_document__.aux"))
    assert cached_aux.read_bytes() == b"known-good-state"


def test_output_limit_has_a_distinct_api_error(compile_client, monkeypatch):
    client, _cache_root = compile_client
    monkeypatch.setattr(
        builds,
        "latexToPdf",
        lambda *_args: {
            **successful_result(),
            "status": "ko",
            "pdf": None,
            "is_output_limit": True,
        },
    )

    response = client.post("/builds/sync", json=request_body("source"))

    assert response.status_code == 400
    assert response.get_json()["error"] == "COMPILATION_OUTPUT_LIMIT"
