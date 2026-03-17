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
from latexonhttp.caching.resources import (
    forward_resource_to_cache,
    get_resource_from_cache,
)
from latexonhttp.caching.bridge import CACHE_HOST
from latexonhttp.workspaces.compile_cache import (
    compute_compile_key,
    acquire_compile_dir,
    release_compile_dir,
    invalidate_compile_dir,
    persist_resource_to_compile_dir,
    clean_stale_outputs,
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
MAX_CONCURRENT_COMPILATIONS = 2


@builds_app.route("/status", methods=["GET"])
def container_status():
    return jsonify({
        "active": _active_compilations,
        "capacity": MAX_CONCURRENT_COMPILATIONS,
    })


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
        logger.info(pprint.pformat(request.args.to_dict(False)))
        input_spec, error = parse_querystring_resources_spec(
            request.args.to_dict(True), request.args.to_dict(False)
        )
        if error:
            return error, 400

    # Support for multipart/form-data requests.
    if request.content_type and "multipart/form-data" in request.content_type:
        input_spec_mode = "multipart/form-data"
        logger.info(request.content_type)
        logger.info(pprint.pformat(request.files))
        logger.info(pprint.pformat(request.form))
        input_spec, error = parse_multipart_resources_spec(request.form, request.files)
        if error:
            return error, 400

    if not input_spec:
        input_spec_mode = "json"
        input_spec, error = parse_json_resources_spec(request.get_json())
        if error:
            return error, 400

    if not input_spec:
        return {"error": "MISSING_COMPILATION_SPECIFICATION"}, 400

    # Payload validations.
    logger.info(request.content_type)
    logger.info(pprint.pformat(request.files))
    logger.info(pprint.pformat(request.form))
    logger.info(input_spec)

    if not input_spec_validator.validate(input_spec):
        return (
            json.dumps(
                {
                    "error": "INVALID_PAYLOAD_SHAPE",
                    "shape_errors": input_spec_validator.errors,
                    "input_spec_mode": input_spec_mode,
                    "input_spec": input_spec,
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

    # -------------
    # Fetching, post-fetch normalization and checks, filesystem creation.
    # -------------

    # Try to use a persistent compile directory so latexmk can reuse
    # auxiliary files (.aux, .fdb_latexmk, .toc, .bbl) from prior
    # compiles, reducing redundant LaTeX passes from ~3 to 1.
    compile_key = compute_compile_key(
        normalized_resources, compilerName, input_spec.get("options")
    )
    use_persistent, compile_dir = acquire_compile_dir(compile_key)

    if use_persistent:
        workspace_id = compile_key
        workspace_dir = compile_dir
        logger.info(
            "Using persistent compile dir %s (key=%s)", compile_dir, compile_key
        )
    else:
        workspace_id = create_workspace(normalized_resources)
        workspace_dir = get_workspace_root_path(workspace_id)
        logger.info("Using ephemeral workspace %s", workspace_id)

    error_in_try_block = None
    error_compilation = False

    try:

        def on_fetched(resource, data):
            logger.debug("Fetched %s: %s bytes", resource["build_path"], len(data))
            resource["data_spec"] = process_resource_data_spec(data)
            if use_persistent:
                error = persist_resource_to_compile_dir(
                    workspace_dir, resource, data
                )
            else:
                error = persist_resource_to_workspace(workspace_id, resource, data)
            if error:
                return error
            is_ok, cache_response = forward_resource_to_cache(resource, data)
            if not is_ok or cache_response:
                if cache_response:
                    logger.warning(
                        "Cache forwarding failed for resource %s: %s",
                        resource.get("build_path", "unknown"),
                        cache_response,
                    )

        cache_provider = get_resource_from_cache if CACHE_HOST else None
        error = fetch_resources(
            normalized_resources, on_fetched, get_from_cache=cache_provider
        )
        if error:
            if use_persistent:
                # Partial writes may have landed; wipe so next request
                # with this key doesn't start from inconsistent state.
                invalidate_compile_dir(compile_key)
            return error, 400

        # -------------
        # Compilation.
        # -------------

        main_resource = next(
            resource
            for resource in normalized_resources
            if resource["is_main_document"]
        )

        # Remove only the specific compiler output files from a prior
        # run so a failed compile can never return a stale PDF.
        # User-supplied PDF assets (logo.pdf, etc.) are NOT deleted.
        if use_persistent:
            clean_stale_outputs(workspace_dir, main_resource["build_path"])
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
                        else "COMPILATION_ERROR"
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

        if response_format == "json":
            return (
                {
                    "status": "success",
                    "pdf": base64.b64encode(latexToPdfOutput["pdf"]).decode(
                        "ascii"
                    ),
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

        if use_persistent:
            if error_in_try_block is not None:
                invalidate_compile_dir(compile_key)
            release_compile_dir(compile_key)
            run_eviction()
        else:
            if KEEP_WORKSPACE_DIR is False and (
                KEEP_WORKSPACE_DIR_ON_ERROR is False
                or (error_in_try_block is None and not error_compilation)
            ):
                remove_workspace(workspace_id)
