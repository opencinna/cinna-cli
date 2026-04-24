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


class PlatformError(CinnaError):
    """Backend returned an unexpected error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Platform error ({status_code}): {detail}")


class MutagenNotFoundError(CinnaError):
    """Mutagen is not installed or not on PATH."""

    def __init__(self, required_version: str | None = None):
        msg = "Mutagen is required but was not found on PATH."
        if required_version:
            msg += f" (required version: {required_version})"
        msg += "\nInstall with:  brew install mutagen-io/mutagen/mutagen"
        msg += "\nOther platforms: https://mutagen.io/documentation/introduction/installation"
        super().__init__(msg)


class MutagenVersionMismatchError(CinnaError):
    """Installed Mutagen version does not match what the platform requires."""

    def __init__(self, installed: str, required: str):
        super().__init__(
            f"Mutagen version mismatch: installed {installed}, platform requires {required}.\n"
            "Upgrade with:  brew upgrade mutagen-io/mutagen/mutagen"
        )
