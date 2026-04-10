"""Workspace synchronization between local and remote environment."""

import fnmatch
import hashlib
import io
import json
import logging
import tarfile
import zipfile
from pathlib import Path

from cinna.config import CinnaConfig, workspace_dir, load_manifest, save_manifest
from cinna.client import PlatformClient
from cinna import console

logger = logging.getLogger("cinna.sync")

# Files/dirs excluded from sync
DEFAULT_EXCLUDES = {
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    "credentials/",
}

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB


def _is_excluded(rel_path: str, excludes: set[str]) -> bool:
    """Check if a relative path matches any exclude pattern."""
    parts = Path(rel_path).parts
    for pattern in excludes:
        # Match directory names
        if pattern.endswith("/"):
            if any(p == pattern.rstrip("/") for p in parts):
                return True
        # Match filename patterns
        elif fnmatch.fnmatch(parts[-1] if parts else "", pattern):
            return True
    return False


def compute_local_manifest(workspace: Path, excludes: set[str] | None = None) -> dict:
    """Compute SHA-256 manifest for all files in the workspace directory.

    Returns: { "relative/path": { "sha256": "...", "size": int, "mtime": float } }

    Skips:
    - Files matching exclude patterns
    - Files > MAX_FILE_SIZE
    - Symlinks (security)
    """
    if excludes is None:
        excludes = DEFAULT_EXCLUDES

    manifest = {}
    if not workspace.is_dir():
        return manifest

    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue

        rel = str(path.relative_to(workspace))

        if _is_excluded(rel, excludes):
            continue

        stat = path.stat()
        if stat.st_size > MAX_FILE_SIZE:
            console.warn(
                f"Skipping large file ({stat.st_size // 1024 // 1024}MB): {rel}"
            )
            continue

        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest[rel] = {
            "sha256": sha256,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }

    return manifest


def diff_manifests(
    local: dict,
    remote: dict,
    last_known: dict,
) -> tuple[list[str], list[str], list[str]]:
    """Compare local, remote, and last-known manifests.

    Returns: (local_changed, remote_changed, conflicts)
    """
    all_files = set(local) | set(remote) | set(last_known)

    local_changed = []
    remote_changed = []
    conflicts = []

    for f in sorted(all_files):
        local_sha = local.get(f, {}).get("sha256")
        remote_sha = remote.get(f, {}).get("sha256")
        last_sha = last_known.get(f, {}).get("sha256")

        l_changed = local_sha != last_sha
        r_changed = remote_sha != last_sha

        if l_changed and r_changed:
            # Both sides changed — conflict (unless they changed to the same thing)
            if local_sha != remote_sha:
                conflicts.append(f)
            # If both changed to same value, no action needed
        elif l_changed:
            local_changed.append(f)
        elif r_changed:
            remote_changed.append(f)

    return local_changed, remote_changed, conflicts


def create_workspace_tarball(workspace: Path, files: list[str] | None = None) -> bytes:
    """Create a gzipped tarball of the workspace (or specific files)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if files is None:
            # Include all non-excluded files
            manifest = compute_local_manifest(workspace)
            files = list(manifest.keys())

        for rel_path in files:
            full_path = workspace / rel_path
            if full_path.is_file() and not full_path.is_symlink():
                tar.add(str(full_path), arcname=rel_path)

    return buf.getvalue()


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
            # Skip directories (they're created implicitly)
            if info.is_dir():
                continue
            if only_files is not None and info.filename not in only_files:
                continue

            # Extract with safe path
            target = workspace / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(info.filename)
    return extracted


def push_workspace(
    client: PlatformClient,
    config: CinnaConfig,
    workspace_root: Path,
    force: bool = False,
) -> None:
    """Push local workspace changes to remote environment."""
    workspace = workspace_dir(workspace_root)
    local_manifest = compute_local_manifest(workspace)
    remote_manifest = client.get_workspace_manifest(config.agent_id).get("files", {})
    last_manifest = load_manifest(workspace_root)

    local_changed, remote_changed, conflicts = diff_manifests(
        local_manifest, remote_manifest, last_manifest
    )

    if conflicts and not force:
        console.warn(f"{len(conflicts)} files changed on both sides:")
        for f in conflicts:
            console.console.print(f"  [yellow]!![/yellow] {f}")
        console.console.print(
            "Skipping conflicted files. Use --force to overwrite remote."
        )
    elif conflicts:
        # Force mode: push our version of conflicted files
        local_changed.extend(conflicts)

    if not local_changed:
        console.status("Nothing to push.")
        return

    with console.spinner(f"Pushing {len(local_changed)} files..."):
        tarball = create_workspace_tarball(workspace, local_changed)
        client.upload_workspace(config.agent_id, tarball)

    # Merge manifest: remote state as base, overlay with local
    merged = {**remote_manifest, **local_manifest}
    save_manifest(merged, workspace_root)
    console.status(f"Pushed {len(local_changed)} files to remote.")


def pull_workspace(
    client: PlatformClient,
    config: CinnaConfig,
    workspace_root: Path,
    force: bool = False,
) -> None:
    """Pull remote workspace changes to local."""
    workspace = workspace_dir(workspace_root)
    remote_manifest = client.get_workspace_manifest(config.agent_id).get("files", {})
    local_manifest = compute_local_manifest(workspace)
    last_manifest = load_manifest(workspace_root)

    local_changed, remote_changed, conflicts = diff_manifests(
        local_manifest, remote_manifest, last_manifest
    )

    if conflicts and not force:
        console.warn(f"{len(conflicts)} files changed on both sides:")
        for f in conflicts:
            console.console.print(f"  [yellow]!![/yellow] {f}")
        console.console.print(
            "Skipping conflicted files. Use --force to overwrite local."
        )
    elif conflicts:
        # Force mode: pull remote version of conflicted files
        remote_changed.extend(conflicts)

    if not remote_changed:
        console.status("Workspace up to date.")
    else:
        with console.spinner(f"Pulling {len(remote_changed)} files..."):
            tarball = client.download_workspace(config.agent_id)
            extract_workspace_tarball(tarball, workspace, only_files=set(remote_changed))
        console.status(f"Pulled {len(remote_changed)} files from remote.")

    # Ensure required dirs exist (files/ may be empty and absent from tarball)
    ensure_workspace_dirs(workspace)

    # Always refresh credentials and building context on pull
    pull_credentials(client, config, workspace_root)
    refresh_building_context(client, config, workspace_root)

    # Update manifest
    merged = {**local_manifest, **remote_manifest}
    save_manifest(merged, workspace_root)


def pull_credentials(
    client: PlatformClient,
    config: CinnaConfig,
    workspace_root: Path,
    quiet: bool = False,
) -> None:
    """Pull credentials from platform and write to workspace/credentials/."""
    creds_data = client.get_credentials(config.agent_id)
    creds_dir = workspace_dir(workspace_root) / "credentials"
    creds_dir.mkdir(parents=True, exist_ok=True)

    # Write credentials.json
    creds_file = creds_dir / "credentials.json"
    creds_json = creds_data.get("credentials", creds_data.get("credentials_json", {}))
    creds_file.write_text(json.dumps(creds_json, indent=2))

    # Write README.md (redacted documentation)
    readme = creds_data.get("credentials_readme", "")
    if readme:
        (creds_dir / "README.md").write_text(readme)

    if not quiet:
        console.status("Credentials updated.")


def refresh_building_context(
    client: PlatformClient,
    config: CinnaConfig,
    workspace_root: Path,
) -> None:
    """Fetch building context and regenerate BUILDING_AGENT.md + CLAUDE.md."""
    from cinna.context import generate_context_files

    with console.spinner("Fetching building context from platform..."):
        ctx = client.get_building_context(config.agent_id)

    generate_context_files(ctx, config, workspace_root)
    console.status("Context files updated.")
