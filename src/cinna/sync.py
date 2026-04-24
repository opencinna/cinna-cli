"""Workspace archive helpers for the initial clone.

Continuous sync is handled by Mutagen (see sync_session.py). What remains here
is just the tarball/zip extraction used once, when `cinna setup` seeds the
workspace from `GET /workspace`.
"""

import io
import logging
import tarfile
import zipfile
from pathlib import Path

from cinna import console

logger = logging.getLogger("cinna.sync")

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB


def ensure_workspace_dirs(workspace: Path) -> None:
    """Ensure required workspace subdirectories exist.

    The remote agent environment always has /app/workspace/files/ but it may be
    empty, so the downloaded tarball won't contain it.  Create it locally so the
    workspace layout matches production.
    """
    (workspace / "files").mkdir(parents=True, exist_ok=True)


def extract_workspace_tarball(
    archive_bytes: bytes,
    workspace: Path,
    only_files: set[str] | None = None,
) -> list[str]:
    """Extract workspace archive to the workspace directory.

    Supports tar (gz/bz2/xz/plain) and zip formats — auto-detected from content.
    Returns list of extracted file paths.
    Validates: no path traversal, no symlinks, max file size.

    If only_files is provided, only extracts files whose relative path is in the set.
    """
    workspace.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        logger.debug("Detected zip archive (%d bytes)", len(archive_bytes))
        return _extract_zip(archive_bytes, workspace, only_files)
    else:
        logger.debug("Detected tar archive (%d bytes)", len(archive_bytes))
        return _extract_tar(archive_bytes, workspace, only_files)


def _extract_tar(
    archive_bytes: bytes,
    workspace: Path,
    only_files: set[str] | None = None,
) -> list[str]:
    """Extract a tar archive (any compression) to workspace."""
    extracted = []
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                console.warn(f"Skipping unsafe path: {member.name}")
                continue
            if member.issym() or member.islnk():
                console.warn(f"Skipping symlink: {member.name}")
                continue
            if member.size > MAX_FILE_SIZE:
                console.warn(f"Skipping large file: {member.name}")
                continue
            if only_files is not None and member.name not in only_files:
                continue

            tar.extract(member, path=workspace, filter="data")
            extracted.append(member.name)
    return extracted


def _extract_zip(
    archive_bytes: bytes,
    workspace: Path,
    only_files: set[str] | None = None,
) -> list[str]:
    """Extract a zip archive to workspace."""
    extracted = []
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        for info in zf.infolist():
            member_path = Path(info.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                console.warn(f"Skipping unsafe path: {info.filename}")
                continue
            if info.file_size > MAX_FILE_SIZE:
                console.warn(f"Skipping large file: {info.filename}")
                continue
            if info.is_dir():
                continue
            if only_files is not None and info.filename not in only_files:
                continue

            target = workspace / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(info.filename)
    return extracted
