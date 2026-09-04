import stat
import sys
import time
from unittest.mock import patch

import psutil
import pytest
from flask import Flask

from latexonhttp.api.builds import builds_app
from latexonhttp import compiler
from latexonhttp.compiler import _compiler_environment, latexToPdf
from latexonhttp.resources.fetching import fetcher_base64_file
from latexonhttp.resources.utils import prune_resources_content_for_logging
from latexonhttp.utils.texlogparser import MAX_PARSED_MESSAGES, parse_latex_log
from latexonhttp.workspaces import filesystem
from latexonhttp.workspaces.filesystem import is_safe_path


@pytest.fixture
def client():
    test_app = Flask(__name__)
    test_app.register_blueprint(builds_app, url_prefix="/builds")
    return test_app.test_client()


def test_workspace_path_check_rejects_sibling_prefix(tmp_path):
    workspace = tmp_path / "job"
    sibling = tmp_path / "job-escape" / "document.tex"

    assert is_safe_path(workspace, workspace / "document.tex")
    assert not is_safe_path(workspace, sibling)


def test_workspace_directories_hide_other_requests(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspaces"
    monkeypatch.setattr(filesystem, "WORKSPACE_DIRECTORY", str(workspace_root))

    filesystem.make_workspace("request-id")

    assert stat.S_IMODE(workspace_root.stat().st_mode) == 0o711
    assert stat.S_IMODE((workspace_root / "request-id").stat().st_mode) == 0o700


def test_compiler_environment_omits_container_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("R2_ACCOUNT_ID", "must-not-leak")

    environment = _compiler_environment(str(tmp_path))

    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "R2_ACCOUNT_ID" not in environment
    assert environment["openin_any"] == "p"
    assert environment["openout_any"] == "p"
    assert environment["shell_escape"] == "f"


def test_latexmk_explicitly_disables_shell_escape(tmp_path):
    main = {
        "build_path": "__main_document__.tex",
        "output_path": "output.pdf",
    }
    (tmp_path / main["build_path"]).write_text("test", encoding="utf-8")
    command_result = {
        "return_code": 1,
        "stdout": "",
        "duration": 0,
        "is_timeout": False,
    }

    with patch("latexonhttp.compiler.run_command", return_value=command_result):
        latexToPdf("lualatex", str(tmp_path), main, "test")

    latexmkrc = (tmp_path / ".latexmkrc").read_text(encoding="utf-8")
    assert "-no-shell-escape" in latexmkrc
    assert "--safer" in latexmkrc


def test_compiler_output_is_bounded_while_the_process_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(compiler, "MAX_COMPILER_OUTPUT_BYTES", 1024)

    result = compiler.run_command(
        str(tmp_path),
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
        timeout=5,
    )

    assert result["return_code"] == 0
    assert len(result["stdout"].encode("utf-8")) <= 1024
    assert "[compiler output truncated]" in result["stdout"]


def test_compiler_timeout_kills_descendants(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    result = compiler.run_command(
        str(tmp_path),
        [sys.executable, "-c", script, str(child_pid_path)],
        timeout=0.5,
    )

    assert result["is_timeout"] is True
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            child = psutil.Process(child_pid)
        except psutil.NoSuchProcess:
            break
        if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
            break
        time.sleep(0.05)
    else:
        psutil.Process(child_pid).kill()
        pytest.fail("compiler descendant survived the process-group timeout")


def test_compiler_workspace_is_bounded_while_process_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(compiler, "MAX_WORKSPACE_BYTES", 64 * 1024)
    monkeypatch.setattr(compiler, "WORKSPACE_MONITOR_INTERVAL_SECONDS", 0.01)
    output_path = tmp_path / "unbounded-output.bin"
    script = (
        "import pathlib, sys, time; "
        "p = pathlib.Path(sys.argv[1]); "
        "f = p.open('wb'); "
        "[(f.write(b'x' * 16384), f.flush(), time.sleep(0.01)) for _ in range(1000)]"
    )

    result = compiler.run_command(
        str(tmp_path),
        [sys.executable, "-c", script, str(output_path)],
        timeout=5,
    )

    assert result["is_workspace_limit"] is True
    assert "Compilation workspace limit exceeded" in result["stdout"]
    assert output_path.stat().st_size < 1024 * 1024


def test_log_enumeration_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(compiler, "MAX_LOG_FILES", 3)
    for index in range(20):
        (tmp_path / f"{index:02}.log").write_text("log", encoding="utf-8")

    assert compiler._bounded_log_names(str(tmp_path)) == [
        "00.log",
        "01.log",
        "02.log",
    ]


def test_workspace_limit_invalidates_an_otherwise_valid_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr(compiler, "MAX_WORKSPACE_BYTES", 1)
    main = {
        "build_path": "__main_document__.tex",
        "output_path": "output.pdf",
    }
    (tmp_path / main["build_path"]).write_text("source", encoding="utf-8")
    (tmp_path / "__main_document__.pdf").write_bytes(b"pdf")
    command_result = {
        "return_code": 0,
        "stdout": "Compilation workspace limit exceeded",
        "duration": 0,
        "is_timeout": False,
        "is_workspace_limit": True,
    }

    with patch("latexonhttp.compiler.run_command", return_value=command_result):
        result = latexToPdf("pdflatex", str(tmp_path), main, "test")

    assert result["status"] == "ko"
    assert result["pdf"] == b"pdf"
    assert result["is_workspace_limit"] is True
    assert result["is_output_limit"] is True


def test_pdf_size_limit_is_reported_as_an_output_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(compiler, "MAX_PDF_BYTES", 2)
    main = {
        "build_path": "__main_document__.tex",
        "output_path": "output.pdf",
    }
    (tmp_path / main["build_path"]).write_text("source", encoding="utf-8")
    (tmp_path / "__main_document__.pdf").write_bytes(b"pdf")
    command_result = {
        "return_code": 0,
        "stdout": "",
        "duration": 0,
        "is_timeout": False,
        "is_workspace_limit": False,
    }

    with patch("latexonhttp.compiler.run_command", return_value=command_result):
        result = latexToPdf("pdflatex", str(tmp_path), main, "test")

    assert result["status"] == "ko"
    assert result["pdf"] is None
    assert result["is_output_limit"] is True


def test_structured_log_messages_are_bounded():
    result = parse_latex_log("\n".join("! repeated error" for _ in range(600)))

    assert result["errors_count"] == 600
    assert len(result["errors"]) == MAX_PARSED_MESSAGES
    assert result["truncated"] is True


def test_invalid_base64_is_a_client_error():
    resource = {
        "build_path": "image.png",
        "body_source": {"raw_base64": "not valid base64%%%"},
    }

    data, error = fetcher_base64_file(resource, None)

    assert data is None
    assert error == {"error": "INVALID_BASE64_RESOURCE", "path": "image.png"}


def test_request_logging_redacts_sources_and_remote_locations():
    redacted = prune_resources_content_for_logging(
        {
            "resources": [
                {
                    "path": "document.tex",
                    "content": "secret source",
                    "url": "https://example.com/file?token=secret",
                }
            ]
        }
    )

    assert redacted["resources"] == [
        {"path": "document.tex", "content": True, "url": True}
    ]


def test_remote_resources_are_disabled_at_compiler_boundary(client):
    response = client.post(
        "/builds/sync",
        json={"resources": [{"url": "http://169.254.169.254/latest/meta-data"}]},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "REMOTE_RESOURCES_DISABLED",
        "unsupported_resource_types": ["url/file"],
    }


def test_cache_scope_must_be_an_opaque_sha256_digest(client):
    response = client.post(
        "/builds/sync",
        json={
            "cache_scope": "raw-user-and-document-id",
            "resources": [{"content": "test"}],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "INVALID_PAYLOAD_SHAPE"


def test_resource_paths_cannot_escape_the_workspace(client):
    response = client.post(
        "/builds/sync",
        json={
            "resources": [
                {"main": True, "content": "test"},
                {"path": "../outside.tex", "content": "secret"},
            ]
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "INVALID_RESOURCE_PATH"}


@pytest.mark.parametrize(
    "resources",
    [
        [
            {"main": True, "content": "main"},
            {"path": "chapter.tex", "content": "first"},
            {"path": "chapter.tex", "content": "second"},
        ],
        [
            {"main": True, "content": "main"},
            {"path": "chapter.tex", "content": "first"},
            {"path": "parts/../chapter.tex", "content": "second"},
        ],
        [
            {"main": True, "content": "main"},
            {"path": "__main_document__.tex", "content": "collision"},
        ],
    ],
)
def test_duplicate_normalized_resource_paths_are_rejected(client, resources):
    response = client.post("/builds/sync", json={"resources": resources})

    assert response.status_code == 400
    assert response.get_json()["error"] == "DUPLICATE_RESOURCE_PATH"
