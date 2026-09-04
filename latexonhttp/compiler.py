# -*- coding: utf-8 -*-
"""
latexonhttp.compiler
~~~~~~~~~~~~~~~~~~~~~
The Latex compiler abstraction.
Get a compilation order (dict task spec) and compiles the order.

:copyright: (c) 2017-2018 Yoan Tournade.
:license: AGPL, see LICENSE for more details.
"""

import subprocess
import os
import grp
import heapq
import pwd
import signal
import threading
import timeit
import logging
import glom
from latexonhttp.utils.texlogparser import parse_latex_log

logger = logging.getLogger(__name__)
# In seconds.
DEFAULT_COMPILE_TIMEOUT = int(os.getenv("DEFAULT_COMPILE_TIMEOUT", 100))
MAX_COMPILER_OUTPUT_BYTES = int(os.getenv("MAX_COMPILER_OUTPUT_BYTES", 2 * 1024 * 1024))
MAX_LOG_FILE_BYTES = int(os.getenv("MAX_LOG_FILE_BYTES", 2 * 1024 * 1024))
MAX_LOG_FILES = int(os.getenv("MAX_LOG_FILES", 10))
MAX_TOTAL_LOG_BYTES = int(os.getenv("MAX_TOTAL_LOG_BYTES", 2 * 1024 * 1024))
MAX_PDF_BYTES = int(os.getenv("MAX_PDF_BYTES", 10 * 1024 * 1024))
MAX_WORKSPACE_BYTES = int(os.getenv("MAX_WORKSPACE_BYTES", 128 * 1024 * 1024))
MAX_WORKSPACE_FILES = int(os.getenv("MAX_WORKSPACE_FILES", 2048))
WORKSPACE_MONITOR_INTERVAL_SECONDS = 0.1
COMPILER_USER = os.getenv("LATEX_COMPILER_USER")
COMPILER_GROUP = os.getenv("LATEX_COMPILER_GROUP")

# TODO Temporary dirty work.
# Lol.
# (Like any Python script that grow indefinitely?)

# TODO Let users access tex, latex, dvilualatex, ptex and uptexfor DVI output.
# https://tex.stackexchange.com/a/397312/122145
# TODO Support also pandoc?
AVAILABLE_LATEX_COMPILERS = [
    "pdflatex",
    "xelatex",
    "lualatex",
    "platex",
    "uplatex",
    "context",
]
AVAILABLE_BIBLIOGRAPHY_COMMANDS = ["bibtex", "biber"]


def _compiler_identity():
    """Resolve the unprivileged account configured for TeX child processes."""
    if not COMPILER_USER:
        return None
    try:
        user = pwd.getpwnam(COMPILER_USER)
        group = grp.getgrnam(COMPILER_GROUP or COMPILER_USER)
    except KeyError as exc:
        raise RuntimeError(
            "Configured LaTeX compiler user/group does not exist"
        ) from exc
    return user.pw_uid, group.gr_gid


def _prepare_compile_directory(directory):
    """Give the unprivileged compiler access only to its local workspace."""
    identity = _compiler_identity()
    if identity is None:
        return {}

    uid, gid = identity
    if os.geteuid() != 0:
        raise RuntimeError(
            "LATEX_COMPILER_USER requires the API process to run as root"
        )

    home = os.path.join(directory, ".compiler-home")
    scratch = os.path.join(directory, ".compiler-tmp")
    os.makedirs(home, exist_ok=True)
    os.makedirs(scratch, exist_ok=True)

    for root, dirnames, filenames in os.walk(directory, followlinks=False):
        if os.path.islink(root):
            raise ValueError("Symbolic links are not allowed in compile workspaces")
        os.chown(root, uid, gid)
        os.chmod(root, 0o700)
        for name in dirnames + filenames:
            path = os.path.join(root, name)
            if os.path.islink(path):
                raise ValueError("Symbolic links are not allowed in compile workspaces")
            os.chown(path, uid, gid)
            os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)

    return {"user": uid, "group": gid, "umask": 0o077}


def _compiler_environment(directory):
    """Build a minimal environment without Worker/container credentials."""
    return {
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": os.path.join(directory, ".compiler-home"),
        "TMPDIR": os.path.join(directory, ".compiler-tmp"),
        "max_print_line": "10000",
        # Kpathsea's paranoid mode rejects absolute and parent-relative document
        # reads/writes while preserving access to the installed TeX tree.
        "openin_any": "p",
        "openout_any": "p",
        "shell_escape": "f",
    }


def _decode_bounded(data, max_bytes, truncated=False, marker=b"\n[output truncated]\n"):
    """Decode at most *max_bytes*, reserving room for a truncation marker."""

    data = bytes(data)
    truncated = truncated or len(data) > max_bytes
    if truncated:
        marker = marker[:max_bytes]
        data = data[: max(0, max_bytes - len(marker))] + marker
    else:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace")


def _bounded_output(value):
    encoded = value.encode("utf-8", errors="replace")
    return _decode_bounded(
        encoded,
        MAX_COMPILER_OUTPUT_BYTES,
        marker=b"\n[compiler output truncated]\n",
    )


def _read_text_limited(path, max_bytes=MAX_LOG_FILE_BYTES):
    with open(path, "rb") as handle:
        data = handle.read(max_bytes + 1)
    return _decode_bounded(
        data,
        max_bytes,
        marker=b"\n[log file truncated]\n",
    )


def _drain_process_output(stream, captured, capture_state):
    """Drain a child pipe continuously while retaining only bounded bytes."""

    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            remaining = MAX_COMPILER_OUTPUT_BYTES - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                capture_state["truncated"] = True
    except (OSError, ValueError):
        # The main thread closes the pipe if a descendant keeps it open.
        pass


def _kill_process_group(process):
    """Kill the isolated process group created for one compilation."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _workspace_exceeds_limits(directory):
    """Check workspace usage without following links or retaining a file list."""

    total_bytes = 0
    total_files = 0
    directories = [directory]
    while directories:
        current = directories.pop()
        try:
            entries = os.scandir(current)
        except FileNotFoundError:
            continue
        except OSError:
            # Fail closed if the workspace cannot be inspected while untrusted
            # compiler code is running.
            return True

        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(entry.path)
                        continue
                    stat_result = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    return True

                total_files += 1
                total_bytes += stat_result.st_size
                if (
                    total_files > MAX_WORKSPACE_FILES
                    or total_bytes > MAX_WORKSPACE_BYTES
                ):
                    return True
    return False


def _monitor_workspace(process, directory, stop_event, monitor_state):
    """Kill a compilation as soon as its workspace crosses a hard quota."""

    while not stop_event.wait(WORKSPACE_MONITOR_INTERVAL_SECONDS):
        if _workspace_exceeds_limits(directory):
            monitor_state["limit_exceeded"] = True
            _kill_process_group(process)
            return


def _bounded_log_names(directory):
    """Return at most MAX_LOG_FILES log names with bounded memory use."""

    with os.scandir(directory) as entries:
        return heapq.nsmallest(
            MAX_LOG_FILES,
            (
                entry.name
                for entry in entries
                if entry.name.endswith(".log") and entry.is_file(follow_symlinks=False)
            ),
        )


def run_command(directory, command, timeout=DEFAULT_COMPILE_TIMEOUT):
    is_timeout = False
    identity_options = _prepare_compile_directory(directory)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=directory,
        env=_compiler_environment(directory),
        start_new_session=True,
        **identity_options,
    )
    captured = bytearray()
    capture_state = {"truncated": False}
    output_reader = threading.Thread(
        target=_drain_process_output,
        args=(process.stdout, captured, capture_state),
        daemon=True,
    )
    output_reader.start()
    monitor_stop = threading.Event()
    monitor_state = {"limit_exceeded": False}
    workspace_monitor = threading.Thread(
        target=_monitor_workspace,
        args=(process, directory, monitor_stop, monitor_state),
        daemon=True,
    )
    workspace_monitor.start()
    started_at = timeit.default_timer()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("Process timeout, killing process group")
        _kill_process_group(process)
        is_timeout = True
        try:
            return_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait()

    # Catch a fast process that crosses the quota between monitor intervals.
    if _workspace_exceeds_limits(directory):
        monitor_state["limit_exceeded"] = True
        _kill_process_group(process)
    monitor_stop.set()
    workspace_monitor.join(timeout=1)

    output_reader.join(timeout=2)
    if output_reader.is_alive() and process.stdout is not None:
        process.stdout.close()
        output_reader.join(timeout=0.2)

    ended_at = timeit.default_timer()
    stdout = _decode_bounded(
        captured,
        MAX_COMPILER_OUTPUT_BYTES,
        truncated=capture_state["truncated"],
        marker=b"\n[compiler output truncated]\n",
    )
    if is_timeout:
        stdout = _bounded_output(stdout + "\nCompilation timeout, process killed\n")
    if monitor_state["limit_exceeded"]:
        stdout = _bounded_output(stdout + "\nCompilation workspace limit exceeded\n")

    logger.debug("Program returned with status code %d", return_code)
    return {
        "return_code": return_code,
        "stdout": stdout,
        "duration": ended_at - started_at,
        "is_timeout": is_timeout,
        "is_workspace_limit": monitor_state["limit_exceeded"],
    }


def latexToPdf(compilerName, directory, main_resource, workspace_id, options=None):
    options = options or {}
    bibtexCommand = glom.glom(options, "bibliography.command", default="bibtex")
    if bibtexCommand not in AVAILABLE_BIBLIOGRAPHY_COMMANDS:
        raise ValueError("Invalid bibtex command")
    if compilerName not in AVAILABLE_LATEX_COMPILERS:
        raise ValueError("Invalid compiler")
    # TODO Choose appropriate options following the compiler.
    # Copy files to tmp directory.
    # Should already be an absolute path (in our usage), but just to be sure.
    directory = os.path.abspath(directory)
    input_path = os.path.join(directory, main_resource["build_path"])
    output_path = os.path.join(
        directory, main_resource["build_path"].replace(".tex", ".pdf")
    )
    logger.info("Compiling %s from %s", main_resource["build_path"], directory)
    if compilerName in ["context"]:
        # Here do not support multi runs or bibtex/biber commands.
        # --> do not pass nonstopmode
        # --> parse jobName / output files from Context output
        # Alternative: support many runners.
        # Arara https://github.com/cereda/arara
        # https://github.com/wtsnjp/llmk
        command = [
            compilerName,
            "--noshellescape",
            main_resource["build_path"],
        ]
    else:
        # Use https://mgeier.github.io/latexmk.html
        # to manage multiple runs of Latex compiler for us.
        # (Cross-references, page numbers, etc.)
        # Create the .latexmkrc configuration file.
        # We enable -synctex=1 (only useful if we
        # return the whole directory).
        # TODO Let the config be provided? (dangerous)
        #  -> After the process is hardened.
        mainLatexCmd = "latex" if compilerName in ["platex", "uplatex"] else "pdflatex"
        # Option to use -halt-on-error to stop on first error.
        halt_on_error = glom.glom(options, "compiler.halt_on_error", default=False)
        silent = glom.glom(options, "compiler.silent", default=False)
        interaction_mode = "batchmode" if silent else "nonstopmode"
        halt_on_error_str = " -halt-on-error" if halt_on_error else ""
        safer_flag = " --safer" if compilerName == "lualatex" else ""
        latexmkrc = f"""${mainLatexCmd} = '{compilerName} -no-shell-escape{safer_flag} -interaction={interaction_mode}{halt_on_error_str} -file-line-error -synctex=1 %O %S';
"""
        logger.debug(".latexmkrc: %s", latexmkrc)
        with open(os.path.join(directory, ".latexmkrc"), "w") as fd:
            fd.write(latexmkrc)
        # As an option, use -silent (aka -interaction=batchmode)
        command = [
            "latexmk",
            "-pdfps" if compilerName in ["platex", "uplatex"] else "-pdf",
        ]
        command += ["-silent"] if silent else []
        command += [main_resource["build_path"]]
    logger.debug(command)
    mainCmdOutput = run_command(directory, command)
    commandOutputs = [mainCmdOutput]
    # if commandOutputs[0]["return_code"] == 0 and compilerName in ["platex", "uplatex"]:
    #     # We need a dvipdfmx pass.
    #     # https://tex.stackexchange.com/questions/295414/what-is-uptex-uplatex
    #     # TODO Use ptex2pdf?
    #     # https://github.com/texjporg/ptex2pdf
    #     command = [
    #         "dvipdfmx",
    #         "{}/{}".format(
    #             log_dir, main_resource["build_path"].replace(".tex", ".dvi")
    #         ),
    #     ]
    #     output_path = "{}/{}".format(
    #         log_dir, main_resource["build_path"].replace(".tex", ".pdf")
    #     )
    #     logger.debug(command)
    #     commandOutputs.append(run_command(log_dir, command))
    # Return both generated PDF and compile logs.
    # TODO Uses workspace.filesystem module read file back?
    pdf = None
    commandsStatusCodes = [
        commandOutput["return_code"] for commandOutput in commandOutputs
    ]
    # We do not check status codes, they are not reliable with Latexmk.
    # all(
    #     commandOutput["return_code"] in [0] for commandOutput in commandOutputs
    # )
    logger.info(output_path)
    pdf_limit_exceeded = False
    if os.path.isfile(output_path):
        output_size = os.path.getsize(output_path)
        if output_size <= MAX_PDF_BYTES:
            with open(output_path, "rb") as f:
                pdf = f.read()
        else:
            pdf_limit_exceeded = True
            logger.warning("Compiled PDF exceeds the %d-byte limit", MAX_PDF_BYTES)
    is_timeout = any(commandOutput["is_timeout"] for commandOutput in commandOutputs)
    is_workspace_limit = any(
        commandOutput.get("is_workspace_limit", False)
        for commandOutput in commandOutputs
    )
    is_output_limit = pdf_limit_exceeded or is_workspace_limit
    status = "ok" if pdf and not is_timeout and not is_output_limit else "ko"
    logger.info("Compilation %s is %s: %s", workspace_id, status, commandsStatusCodes)
    log_files = {}
    remaining_log_bytes = MAX_TOTAL_LOG_BYTES
    # Get the log files.
    for log_path in _bounded_log_names(directory):
        if remaining_log_bytes <= 0:
            break
        log_limit = min(MAX_LOG_FILE_BYTES, remaining_log_bytes)
        log_value = _read_text_limited(os.path.join(directory, log_path), log_limit)
        log_files[log_path] = log_value
        remaining_log_bytes -= len(log_value.encode("utf-8", errors="replace"))

    logs = _bounded_output(
        "\n".join(commandOutput["stdout"] for commandOutput in commandOutputs)
    )

    # Parse the primary .log file for structured error/warning extraction.
    # Fall back to stdout logs if no .log file exists.
    primary_log_name = main_resource["build_path"].replace(".tex", ".log")
    log_to_parse = log_files.get(primary_log_name, logs)
    parsed_log = parse_latex_log(log_to_parse)

    return {
        "status": status,
        "pdf": pdf,
        "log_files": log_files,
        "output_path": main_resource["output_path"],
        "duration": sum(commandOutput["duration"] for commandOutput in commandOutputs),
        "logs": logs,
        "is_timeout": is_timeout,
        "is_workspace_limit": is_workspace_limit,
        "is_output_limit": is_output_limit,
        "return_codes": commandsStatusCodes,
        "parsed_log": parsed_log,
    }
