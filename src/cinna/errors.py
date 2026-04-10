"""Custom exceptions for cinna CLI."""

import click


class CinnaError(click.ClickException):
    """Base exception — all cinna errors are Click exceptions so they display nicely."""


class ConfigNotFoundError(CinnaError):
    """No .cinna/config.json found. User needs to run setup."""

    def __init__(self):
        super().__init__(
            "Not in a cinna workspace. Run the setup command from the platform UI first."
        )


class AuthenticationError(CinnaError):
    """CLI token rejected by the platform."""

    def __init__(self, detail: str = ""):
        msg = "Authentication failed. Your session may have expired."
        if detail:
            msg += f" ({detail})"
        msg += "\nRun the setup command again from the platform UI."
        super().__init__(msg)


class DockerNotFoundError(CinnaError):
    """Docker is not installed."""

    def __init__(self):
        super().__init__(
            "Docker is required but not found. Install: https://docs.docker.com/get-docker/"
        )


class ContainerNotRunningError(CinnaError):
    """Container exists but is not running."""


class PlatformError(CinnaError):
    """Backend returned an unexpected error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Platform error ({status_code}): {detail}")


class SyncConflictError(CinnaError):
    """Push/pull conflict detected."""
