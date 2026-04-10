"""Docker build and container lifecycle management."""

import io
import json
import logging
import subprocess
import tarfile
import zipfile
from pathlib import Path

import click

from cinna.config import CinnaConfig, build_dir, load_config
from cinna.errors import DockerNotFoundError
from cinna import console

logger = logging.getLogger("cinna.docker")


def check_docker_available() -> None:
    """Verify docker and docker compose are installed. Raises DockerNotFoundError."""
    for cmd in [["docker", "--version"], ["docker", "compose", "version"]]:
        try:
            subprocess.run(cmd, capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            raise DockerNotFoundError()


def extract_build_context(archive_bytes: bytes, workspace_root: Path) -> None:
    """Extract build context archive to .cinna/build/. Supports tar (any compression) and zip."""
    dest = build_dir(workspace_root)
    dest.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        logger.debug("Build context: detected zip archive")
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            for info in zf.infolist():
                member_path = Path(info.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise click.ClickException(
                        f"Refusing to extract path with traversal: {info.filename}"
                    )
            zf.extractall(path=dest)
    else:
        logger.debug("Build context: detected tar archive")
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
            for member in tar.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise click.ClickException(
                        f"Refusing to extract path with traversal: {member.name}"
                    )
            tar.extractall(path=dest, filter="data")


def ensure_dev_compose_override(workspace_root: Path) -> None:
    """Write docker-compose.override.yml for local dev.

    Overrides:
    - entrypoint: sleep infinity (idle sandbox instead of production server)
    - container_name: from config (includes agent ID to avoid conflicts)
    - image: cinna-dev-<container_name> (descriptive, unique)
    """
    build = build_dir(workspace_root)
    override_path = build / "docker-compose.override.yml"
    compose_path = build / "docker-compose.yml"
    if not compose_path.exists():
        compose_path = build / "compose.yml"
    if not compose_path.exists():
        return

    # Load config for container/image naming
    try:
        config = load_config(workspace_root)
        container_name = config.container_name
        image_name = f"cinna-dev-{container_name}"
    except Exception:
        container_name = None
        image_name = None

    # Ask docker compose for the service names
    result = subprocess.run(
        ["docker", "compose", "config", "--services"],
        cwd=str(build),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Could not read compose services: %s", result.stderr)
        return

    services = result.stdout.strip().splitlines()
    if not services:
        return

    # Resolve the workspace directory as an absolute path so the volume mount
    # works correctly regardless of the compose file's location (.cinna/build/).
    from cinna.config import workspace_dir

    ws_abs = str(workspace_dir(workspace_root).resolve())

    lines = ["services:"]
    for svc in services:
        lines.append(f"  {svc}:")
        lines.append('    entrypoint: ["sleep", "infinity"]')
        lines.append("    command: []")
        lines.append("    volumes:")
        lines.append(f"      - {ws_abs}:/app/workspace")
        if container_name:
            lines.append(f"    container_name: {container_name}")
        if image_name:
            lines.append(f"    image: {image_name}")

    override_path.write_text("\n".join(lines) + "\n")
    logger.info("Wrote dev compose override for services: %s", services)


def build_container(workspace_root: Path, no_cache: bool = False) -> None:
    """Run docker compose build in the build context directory."""
    build = build_dir(workspace_root)
    cmd = ["docker", "compose", "build"]
    if no_cache:
        cmd.append("--no-cache")

    with console.spinner("Building container..."):
        result = subprocess.run(
            cmd,
            cwd=str(build),
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        console.error("Container build failed:")
        console.console.print(result.stderr)
        raise click.Abort()


def start_container(workspace_root: Path) -> None:
    """Create and start the container via docker compose up -d."""
    ensure_dev_compose_override(workspace_root)
    build = build_dir(workspace_root)
    subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=str(build),
        capture_output=True,
        check=True,
    )


def destroy_container(workspace_root: Path) -> None:
    """Stop and remove container + network. Containers are stateless — always remove."""
    build = build_dir(workspace_root)
    if not build.is_dir():
        return
    subprocess.run(
        ["docker", "compose", "down"],
        cwd=str(build),
        capture_output=True,
    )


def remove_images(workspace_root: Path) -> None:
    """Remove container, network, volumes, and locally-built images (not base/pulled images)."""
    build = build_dir(workspace_root)
    if not build.is_dir():
        return
    subprocess.run(
        [
            "docker",
            "compose",
            "down",
            "--rmi",
            "local",
            "--volumes",
            "--remove-orphans",
        ],
        cwd=str(build),
        capture_output=True,
    )


def _get_compose_container_name(workspace_root: Path) -> str | None:
    """Get the actual container name created by docker compose."""
    build = build_dir(workspace_root)
    if not build.is_dir():
        return None
    result = subprocess.run(
        ["docker", "compose", "ps", "-q"],
        cwd=str(build),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    container_id = result.stdout.strip().splitlines()[0]
    # Resolve to container name
    name_result = subprocess.run(
        ["docker", "inspect", "-f", "{{.Name}}", container_id],
        capture_output=True,
        text=True,
    )
    if name_result.returncode != 0:
        return None
    return name_result.stdout.strip().lstrip("/")


def is_container_running(workspace_root: Path) -> bool:
    """Check if the agent container is running via docker compose."""
    build = build_dir(workspace_root)
    if not build.is_dir():
        return False
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", "--filter", "status=running"],
        cwd=str(build),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def exec_in_container(
    config: CinnaConfig, command: list[str], workspace_root: Path
) -> int:
    """Run a command inside the container via docker exec.

    Returns the exit code. Streams stdout/stderr directly to terminal.
    Requires the container to be running (via 'cinna dev' or 'cinna env-up').
    """
    container_name = _get_compose_container_name(workspace_root)
    if not container_name:
        raise click.ClickException(
            "Container is not running.\n"
            "Start it with 'cinna dev' (interactive) or 'cinna env-up' (background)."
        )

    import sys

    docker_cmd = ["docker", "exec"]
    if sys.stdin.isatty():
        docker_cmd.append("-it")
    else:
        docker_cmd.append("-i")
    docker_cmd.append(container_name)
    docker_cmd.extend(command)

    result = subprocess.run(docker_cmd)
    return result.returncode


def get_container_status(config: CinnaConfig, workspace_root: Path) -> dict:
    """Get container status info for display."""
    build = build_dir(workspace_root)
    if not build.is_dir():
        return {"running": False, "image": "", "created": "", "status": "not found", "id": "", "name": ""}

    # Use docker compose ps --format json for reliable structured output
    result = subprocess.run(
        ["docker", "compose", "ps", "--format", "json", "-a"],
        cwd=str(build),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"running": False, "image": "", "created": "", "status": "not found", "id": "", "name": ""}

    # docker compose ps --format json outputs either a JSON array (older versions)
    # or one JSON object per line (newer versions)
    raw = result.stdout.strip()
    entries = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            entries = parsed
        elif isinstance(parsed, dict):
            entries = [parsed]
    except json.JSONDecodeError:
        # Fallback: one JSON object per line
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if entries:
        entry = entries[0]
        state = entry.get("State", "")
        return {
            "running": state == "running",
            "image": entry.get("Image", ""),
            "created": "",
            "status": state,
            "id": entry.get("ID", ""),
            "name": entry.get("Name", ""),
        }

    return {"running": False, "image": "", "created": "", "status": "not found", "id": "", "name": ""}
