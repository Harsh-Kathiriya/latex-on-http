# -*- coding: utf-8 -*-
"""
latexonhttp.workspaces.filesystem
~~~~~~~~~~~~~~~~~~~~~
Filesystem driver / management for build workspaces.

:copyright: (c) 2019 Yoan Tournade.
:license: AGPL, see LICENSE for more details.
"""

import logging
import os.path
import shutil

logger = logging.getLogger(__name__)

CHECK_DATA_SPEC_SIZE_ESTIMATE = True
WORKSPACE_DIRECTORY = "./tmp/loh_workspaces"

# TODO Clean workspace directory on start/init?
# --> if there was an application error, the workspace is orphaned and not removed.


def is_safe_path(basedir, path, follow_symlinks=False):
    """Return whether ``path`` is contained by ``basedir``.

    A raw string-prefix check treats sibling paths such as ``/tmp/job-2`` as
    children of ``/tmp/job``.  ``commonpath`` compares actual path components
    and therefore closes that traversal edge case.
    """
    normalize = os.path.realpath if follow_symlinks else os.path.abspath
    base = normalize(basedir)
    candidate = normalize(path)
    try:
        return os.path.commonpath((base, candidate)) == base
    except ValueError:
        # Different drives on Windows, or another malformed path combination.
        return False


def get_workspace_root_path(workspace_id):
    return os.path.abspath("{}/{}".format(WORKSPACE_DIRECTORY, workspace_id))


def get_resource_fullpath(workspace_id, resource):
    return os.path.abspath(
        "{}/{}".format(get_workspace_root_path(workspace_id), resource["build_path"])
    )


def persist_resource_to_workspace(workspace_id, resource, data):
    resource_full_path = get_resource_fullpath(workspace_id, resource)
    if not is_safe_path(get_workspace_root_path(workspace_id), resource_full_path):
        return "INVALID_PATH"
    # TODO Id for identifying input resources.
    logger.info("Writing to %s ...", resource_full_path)
    os.makedirs(os.path.dirname(resource_full_path), exist_ok=True)
    with open(resource_full_path, "wb") as f:
        bytes_written = f.write(data)
        logger.debug("Wrote %d bytes to %s", bytes_written, resource_full_path)
    if CHECK_DATA_SPEC_SIZE_ESTIMATE:
        check_data_spec_size_estimate(workspace_id, resource)


def make_workspace(workspace_id):
    workspace_path = get_workspace_root_path(workspace_id)
    logger.info("Creating workspace directory %s", workspace_path)
    # The root is searchable but not listable. Request workspaces remain
    # root-only until the compiler explicitly hands the active one to the
    # unprivileged TeX account.
    os.makedirs(WORKSPACE_DIRECTORY, mode=0o711, exist_ok=True)
    os.chmod(WORKSPACE_DIRECTORY, 0o711)
    os.mkdir(workspace_path, mode=0o700)


def delete_workspace(workspace_id):
    workspace_path = get_workspace_root_path(workspace_id)
    logger.info("Deleting workspace directory %s", workspace_path)
    shutil.rmtree(workspace_path)


def check_data_spec_size_estimate(workspace_id, resource):
    size_on_disk = os.path.getsize(get_resource_fullpath(workspace_id, resource))
    logger.debug(
        "Size on disk: %d ft. Size est: %s", size_on_disk, resource["data_spec"]["size"]
    )
    if size_on_disk != resource["data_spec"]["size"]:
        logger.warning(
            "Resource %s size estimate (%s) mismatch with size on disk (%d)",
            resource["data_spec"]["size"],
            size_on_disk,
            resource["build_path"],
        )
