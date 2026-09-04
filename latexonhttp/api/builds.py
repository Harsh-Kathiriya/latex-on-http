# -*- coding: utf-8 -*-
"""
latexonhttp.api.builds
~~~~~~~~~~~~~~~~~~~~~
Manage Latex builds / compilations.

:copyright: (c) 2017-2019 Yoan Tournade.
:license: AGPL, see LICENSE for more details.
"""

import base64
import envparse
import logging
import os
import pprint
import json
import threading
import glom
import cerberus
from flask import Blueprint, jsonify, request
from latexonhttp.compiler import (
    latexToPdf,
    AVAILABLE_LATEX_COMPILERS,
    AVAILABLE_BIBLIOGRAPHY_COMMANDS,
)
from latexonhttp.resources.normalization import normalize_resources_input
from latexonhttp.resources.validation import check_resources_prefetch
from latexonhttp.resources.fetching import fetch_resources
from latexonhttp.resources.utils import (
    process_resource_data_spec,
    prune_resources_content_for_logging,
)
from latexonhttp.resources.multipart_api import parse_multipart_resources_spec
from latexonhttp.resources.querystring_api import parse_querystring_resources_spec
from latexonhttp.resources.json_api import parse_json_resources_spec
from latexonhttp.workspaces.lifecycle import create_workspace, remove_workspace
from latexonhttp.workspaces.filesystem import (
    get_workspace_root_path,
    persist_resource_to_workspace,
)
from latexonhttp.workspaces.compile_cache import (
    compute_compile_key,
    validate_cache_scope,
    acquire_cache_lock,
    release_cache_lock,
    restore_compile_state,
    publish_compile_state,
    run_eviction,
)

logger = logging.getLogger(__name__)

builds_app = Blueprint("builds", __name__)
KEEP_WORKSPACE_DIR = envparse.env("KEEP_WORKSPACE_DIR", cast=bool, default=False)
KEEP_WORKSPACE_DIR_ON_ERROR = envparse.env(
    "KEEP_WORKSPACE_DIR_ON_ERROR", cast=bool, default=False
)

_active_compilations = 0
_compilations_lock = threading.Lock()
# All TeX children use the same locked-down Unix account. Serial execution
# keeps one tenant's active workspace inaccessible to every other tenant.
MAX_CONCURRENT_COMPILATIONS = 1
COMPILE_SLOT_WAIT_SECONDS = envparse.env(
    "COMPILE_SLOT_WAIT_SECONDS", cast=int, default=8
)
MAX_RESOURCES = envparse.env("MAX_RESOURCES", cast=int, default=100)
MAX_RESOURCE_BYTES = envparse.env(
    "MAX_RESOURCE_BYTES", cast=int, default=10 * 1024 * 1024
)
MAX_TOTAL_RESOURCE_BYTES = envparse.env(
    "MAX_TOTAL_RESOURCE_BYTES", cast=int, default=24 * 1024 * 1024
)
_compile_slots = threading.BoundedSemaphore(MAX_CONCURRENT_COMPILATIONS)


@builds_app.route("/status", methods=["GET"])
def container_status():
    return jsonify(
        {
            "active": _active_compilations,
            "capacity": MAX_CONCURRENT_COMPILATIONS,
        }
    )


# TODO Extract the filesystem/workspace management in a module:
# - determine of fs/files actions to get to construct the filesystem;
# - support content/string, base64/file, url/file, url/git, url/tar, post-data/tar
# - hash and make a (deterministic) signature of files uploaded;
# - from the list of actions, prepare the file system (giving only a root directory);
# (- add a cache management on the file system preparation subpart).
#
# The compiler only uses:
# - the hash for an eventual output cache
# (if entire input signature match a cached output file, just return this file);
# - the prepared directory of files where the build happens.

# Persist cached files.
# Endpoint for checking if inputs (or output) are cached,
# for smart client use.

# TODO Only register request here, and allows to define an hook for when
# the work is done?
# Allows the two: (async, sync)
# TODO Returns the build job id in a response HTTP header.
# TODO Make a commond implementation for both async and sync, using a message broker.
# TODO Store jobs in a Redis, to be flushed.
# TODO With this job store, add top-level cache:
# signature of compilation spec -> in cache? -> directly return.


class JSONInputSpecEncoderForDebug(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):
            return "<binary-content>"
        return json.JSONEncoder.default(self, obj)


input_spec_schema = {
    "compiler": {"type": "string", "allowed": AVAILABLE_LATEX_COMPILERS},
    "cache_scope": {
        "type": "string",
        "regex": "^[0-9a-f]{64}$",
        "maxlength": 64,
    },
    "resources": {
        "type": "list",
        "required": True,
        "schema": {
            "type": "dict",
            # For now, we just check the keys.
            "keysrules": {
                "type": "string",
                "allowed": [
                    "url",
                    "file",
                    "git",
                    "tar",
                    "cache",
                    "content",
                    "main",
                    "path",
                ],
            },
        },
    },
    "options": {
        "type": "dict",
        "schema": {
            "bibliography": {
                "type": "dict",
                "schema": {
                    "command": {
                        "type": "string",
                        "allowed": AVAILABLE_BIBLIOGRAPHY_COMMANDS,
                    }
                },
            },
            "response": {
                "type": "dict",
                "schema": {
                    "log_files_on_failure": {
                        "type": ["boolean", "string", "integer"],
                    },
                    "format": {
                        "type": "string",
                        "allowed": ["pdf", "json"],
                    },
                },
            },
            "compiler": {
                "type": "dict",
                "schema": {
                    "halt_on_error": {
                        "type": ["boolean", "string", "integer"],
                    },
                    "silent": {
                        "type": ["boolean", "string", "integer"],
                    },
                },
            },
        },
    },
}
input_spec_validator = cerberus.Validator(input_spec_schema)


def parse_bool_str_arg(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() in ["true", "1", "t"]


def is_safe_relative_resource_path(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if "\0" in value or "\r" in value or "\n" in value:
        return False
    normalized = os.path.normpath(value.replace("\\", "/"))
    return not (
        normalized in (".", "..")
        or normalized.startswith("../")
        or normalized.startswith("/")
    )


def normalized_resource_path(value):
    """Return the path identity used when a resource is written to disk."""

    return os.path.normpath(value.replace("\\", "/"))


@builds_app.route("/sync", methods=["GET", "POST"])
def compiler_latex():
    input_spec = None

    # TODO Allows mixed APIs?
    # for eg. using GET/param to specify the compiler
    # with a POST/json payload (POST:/builds/sync?compiler=xelatex)

    input_spec_mode = None
    # Support for GET querystring requests.
    if request.method == "GET":
        input_spec_mode = "querystring"
        input_spec, error = parse_querystring_resources_spec(
            request.args.to_dict(True), request.args.to_dict(False)
        )
        if error:
            return error, 400

    # Support for multipart/form-data requests.
    if request.content_type and "multipart/form-data" in request.content_type:
        input_spec_mode = "multipart/form-data"
        input_spec, error = parse_multipart_resources_spec(request.form, request.files)
        if error:
            return error, 400

    if not input_spec:
        input_spec_mode = "json"
        input_spec, error = parse_json_resources_spec(request.get_json(silent=True))
        if error:
            return error, 400

    if not input_spec:
        return {"error": "MISSING_COMPILATION_SPECIFICATION"}, 400

    # Payload validations.
    logger.info(
        "Received %s compilation request (%s)",
        input_spec_mode,
        request.content_type or "no content type",
    )

    if not input_spec_validator.validate(input_spec):
        return (
            json.dumps(
                {
                    "error": "INVALID_PAYLOAD_SHAPE",
                    "shape_errors": input_spec_validator.errors,
                    "input_spec_mode": input_spec_mode,
                },
                cls=JSONInputSpecEncoderForDebug,
            ),
            400,
            {"Content-Type": "application/json"},
        )

    # High-level normalizsation.
    logger.info(
        "Before normalization %s",
        pprint.pformat(prune_resources_content_for_logging(input_spec)),
    )

    # - compiler
    # Choose compiler: latex, pdflatex, xelatex or lualatex
    # We default to pdflatex.
    compilerName = input_spec.get("compiler", "pdflatex")

    # -options.bibliography.command
    # Choose bibliography command: bibtex, biber.
    # We default to bibtex.
    glom.assign(
        input_spec,
        "options.bibliography.command",
        glom.glom(input_spec, "options.bibliography.command", default="bibtex"),
        missing=dict,
    )
    # -options.compiler.halt_on_error
    glom.assign(
        input_spec,
        "options.compiler.halt_on_error",
        parse_bool_str_arg(
            glom.glom(input_spec, "options.compiler.halt_on_error", default=False)
        ),
        missing=dict,
    )
    # -options.compiler.silent
    glom.assign(
        input_spec,
        "options.compiler.silent",
        parse_bool_str_arg(
            glom.glom(input_spec, "options.compiler.silent", default=False)
        ),
        missing=dict,
    )
    # -options.log_files_on_failure
    glom.assign(
        input_spec,
        "options.response.log_files_on_failure",
        parse_bool_str_arg(
            glom.glom(input_spec, "options.response.log_files_on_failure", default=True)
        ),
        missing=dict,
    )
    # -options.response.format ("pdf" for backward compat, "json" for structured response)
    glom.assign(
        input_spec,
        "options.response.format",
        glom.glom(input_spec, "options.response.format", default="pdf"),
        missing=dict,
    )

    # Pre-normalized data checks.

    # - resources (mandatory, must be an array).
    if "resources" not in input_spec:
        return {"error": "MISSING_RESOURCES"}, 400
    if type(input_spec["resources"]) is not list:
        return {"error": "RESOURCES_SPEC_MUST_BE_A_LIST"}, 400

    # - compiler
    if compilerName not in AVAILABLE_LATEX_COMPILERS:
        return (
            {
                "error": "INVALID_COMPILER",
                "available_compilers": AVAILABLE_LATEX_COMPILERS,
            },
            400,
        )

    # -options.bibliography.command
    if (
        glom.glom(input_spec, "options.bibliography.command")
        not in AVAILABLE_BIBLIOGRAPHY_COMMANDS
    ):
        return (
            {
                "error": "INVALID_BILIOGRAPHY_COMMAND",
                "available_commands": AVAILABLE_BIBLIOGRAPHY_COMMANDS,
            },
            400,
        )

    # -------------
    # Pre-fetch normalization and checks.
    # -------------

    normalized_resources = normalize_resources_input(input_spec["resources"])
    # if logger.isEnabledFor(logging.DEBUG):
    #     logger.debug(pprint.pformat(normalized_resources))
    # - Prefetch checks (paths, main document, ...);
    errors = check_resources_prefetch(normalized_resources)
    if errors:
        return errors[0], 400

    if len(normalized_resources) > MAX_RESOURCES:
        return {"error": "TOO_MANY_RESOURCES", "max_resources": MAX_RESOURCES}, 400

    unsupported_types = sorted(
        {
            resource["type"]
            for resource in normalized_resources
            if resource["type"] not in {"utf8/string", "base64/file"}
        }
    )
    if unsupported_types:
        return (
            {
                "error": "REMOTE_RESOURCES_DISABLED",
                "unsupported_resource_types": unsupported_types,
            },
            400,
        )

    seen_resource_paths = set()
    for resource in normalized_resources:
        paths = (resource.get("build_path"), resource.get("output_path"))
        if any(
            path is not None and not is_safe_relative_resource_path(path)
            for path in paths
        ):
            return {"error": "INVALID_RESOURCE_PATH"}, 400
        build_path = normalized_resource_path(resource["build_path"])
        if build_path in seen_resource_paths:
            return {"error": "DUPLICATE_RESOURCE_PATH", "path": build_path}, 400
        seen_resource_paths.add(build_path)

    cache_scope = input_spec.get("cache_scope")
    if cache_scope is not None and not validate_cache_scope(cache_scope):
        return {"error": "INVALID_CACHE_SCOPE"}, 400

    # Every compilation gets a fresh local workspace. R2 is only a backing
    # store for selected auxiliary files, never a place where untrusted TeX
    # runs or submitted documents are stored.
    workspace_id = create_workspace(normalized_resources)
    workspace_dir = get_workspace_root_path(workspace_id)
    compile_key = compute_compile_key(
        normalized_resources,
        compilerName,
        input_spec.get("options"),
        cache_scope=cache_scope,
    )
    cache_locked = acquire_cache_lock(compile_key)
    cache_restored = False
    compile_slot_acquired = False
    error_in_try_block = None
    error_compilation = False

    try:
        if cache_locked:
            cache_restored = restore_compile_state(compile_key, workspace_dir)
            logger.info(
                "Compile cache %s for key %s",
                "restored" if cache_restored else "missed",
                compile_key[:12],
            )

        total_resource_bytes = 0

        def on_fetched(resource, data):
            nonlocal total_resource_bytes
            resource_size = len(data)
            if resource_size > MAX_RESOURCE_BYTES:
                return {
                    "error": "RESOURCE_TOO_LARGE",
                    "path": resource.get("build_path"),
                    "max_bytes": MAX_RESOURCE_BYTES,
                }
            total_resource_bytes += resource_size
            if total_resource_bytes > MAX_TOTAL_RESOURCE_BYTES:
                return {
                    "error": "RESOURCES_TOO_LARGE",
                    "max_total_bytes": MAX_TOTAL_RESOURCE_BYTES,
                }

            logger.debug("Fetched %s: %s bytes", resource["build_path"], resource_size)
            resource["data_spec"] = process_resource_data_spec(data)
            return persist_resource_to_workspace(workspace_id, resource, data)

        error = fetch_resources(normalized_resources, on_fetched)
        if error:
            return error, 400

        main_resource = next(
            resource
            for resource in normalized_resources
            if resource["is_main_document"]
        )

        if not _compile_slots.acquire(timeout=COMPILE_SLOT_WAIT_SECONDS):
            error_compilation = True
            return (
                {"error": "COMPILER_BUSY", "retry_after_seconds": 2},
                503,
                {"Retry-After": "2"},
            )
        compile_slot_acquired = True

        global _active_compilations
        with _compilations_lock:
            _active_compilations += 1
        try:
            latexToPdfOutput = latexToPdf(
                compilerName,
                workspace_dir,
                main_resource,
                workspace_id,
                input_spec["options"],
            )
        finally:
            with _compilations_lock:
                _active_compilations -= 1

        # -------------
        # Response creation.
        # -------------

        response_format = glom.glom(
            input_spec, "options.response.format", default="pdf"
        )
        include_log_files = glom.glom(
            input_spec, "options.response.log_files_on_failure"
        )
        parsed_log = latexToPdfOutput["parsed_log"]

        if latexToPdfOutput["status"] != "ok":
            error_compilation = True
            return (
                {
                    "error": (
                        "COMPILATION_TIMEOUT"
                        if latexToPdfOutput["is_timeout"]
                        else (
                            "COMPILATION_OUTPUT_LIMIT"
                            if latexToPdfOutput.get("is_output_limit", False)
                            else "COMPILATION_ERROR"
                        )
                    ),
                    "duration": round(latexToPdfOutput["duration"], 2),
                    "logs": latexToPdfOutput["logs"],
                    "parsed_log": parsed_log,
                    **(
                        {"log_files": latexToPdfOutput["log_files"]}
                        if include_log_files or response_format == "json"
                        else {}
                    ),
                },
                400,
            )

        if cache_locked:
            submitted_paths = {
                resource["build_path"] for resource in normalized_resources
            }
            if not publish_compile_state(compile_key, workspace_dir, submitted_paths):
                logger.warning(
                    "Unable to publish compile cache key %s", compile_key[:12]
                )

        if response_format == "json":
            return (
                {
                    "status": "success",
                    "pdf": base64.b64encode(latexToPdfOutput["pdf"]).decode("ascii"),
                    "output_filename": latexToPdfOutput["output_path"],
                    "duration": round(latexToPdfOutput["duration"], 2),
                    "logs": latexToPdfOutput["logs"],
                    "parsed_log": parsed_log,
                    "log_files": latexToPdfOutput["log_files"],
                },
                201,
            )

        return (
            latexToPdfOutput["pdf"],
            201,
            {
                "Content-Type": "application/pdf",
                "Content-Disposition": "inline;filename={}".format(
                    latexToPdfOutput["output_path"]
                ),
            },
        )

    except Exception as e:
        error_in_try_block = e
        logger.exception(e)
        return ({"error": "SERVER_ERROR"}, 500)

    finally:
        # -------------
        # Cleanup.
        # -------------

        try:
            if cache_locked:
                release_cache_lock(compile_key)
            run_eviction()
            if KEEP_WORKSPACE_DIR is False and (
                KEEP_WORKSPACE_DIR_ON_ERROR is False
                or (error_in_try_block is None and not error_compilation)
            ):
                remove_workspace(workspace_id)
        finally:
            # Keep the slot until this request's compiler-owned workspace is
            # removed. All TeX children share one unprivileged Unix account,
            # so overlapping workspaces would weaken tenant isolation.
            if compile_slot_acquired:
                _compile_slots.release()
