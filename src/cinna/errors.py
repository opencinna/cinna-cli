"""Custom exceptions for cinna CLI.

Every error the CLI raises on purpose is a ``click.ClickException`` so Click
renders it (``Error: …`` on stderr) and exits non-zero. ``CinnaExit`` adds the
two things a *driver* process (Cinna Desktop spawning ``cinna`` with no TTY)
needs on top of the human message: a **stable process exit code** and a
**machine-readable error code**. The entry point in ``cinna.main`` maps any
other ``ClickException`` onto ``CinnaExit`` so the contract holds for every
command, not only the ones that raise ``CinnaExit`` directly.

Exit codes (see ``docs/features/bootstrap_onboarding``):

  0   success
  1   any other error — ``code`` says which (``needs_input``,
      ``workspace_exists``, ``mutagen_missing``, ``mutagen_mismatch``, …)
  10  setup token invalid / expired / already used (the exchange's 4xx)
  11  the exchanged token belongs to a different account than the workspace
  12  the platform could not be reached, or answered 5xx
"""

import click

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_SETUP_TOKEN = 10
EXIT_ACCOUNT_MISMATCH = 11
EXIT_NETWORK = 12


class CinnaError(click.ClickException):
    """Base exception — all cinna errors are Click exceptions so they display nicely."""


class CinnaExit(CinnaError):
    """An error with a stable process exit code and a machine-readable code.

    ``exit_code`` is what the process exits with; ``code`` is the short
    snake_case identifier a driver switches on; ``detail`` (== ``message``) is
    the human text. In ``--json`` mode ``show()`` prints the final
    ``{"result": "error", "code": …, "detail": …}`` line on stdout instead of
    the ``Error: …`` line on stderr; ``extra`` fields are merged into it.
    """

    def __init__(
        self,
        exit_code: int,
        code: str,
        detail: str,
        *,
        extra: dict | None = None,
    ):
        super().__init__(detail)
        self.exit_code = exit_code
        self.code = code
        self.detail = detail
        self.extra = dict(extra or {})

    def as_json(self) -> dict:
        payload = {"result": "error", "code": self.code, "detail": self.detail}
        payload.update(self.extra)
        return payload

    def show(self, file=None) -> None:
        from cinna import console

        if console.json_mode:
            console.emit_json(self.as_json())
            return
        super().show(file=file)

    @classmethod
    def from_click(cls, exc: click.ClickException) -> "CinnaExit":
        """Wrap a plain ClickException so it carries a code + exit code."""
        if isinstance(exc, cls):
            return exc
        if isinstance(exc, click.UsageError):
            # Click's own exit code for bad invocations; keep it distinct so a
            # driver can tell "you called me wrong" from "the operation failed".
            return cls(EXIT_USAGE, "usage", exc.format_message())
        return cls(
            getattr(exc, "exit_code", EXIT_ERROR) or EXIT_ERROR,
            getattr(exc, "code", None) or "error",
            exc.format_message(),
        )


class NeedsInputError(CinnaExit):
    """A prompt was required but ``--no-input`` (or CINNA_NO_INPUT=1) is set."""

    def __init__(self, prompt: str):
        super().__init__(
            EXIT_ERROR,
            "needs_input",
            f"Input required but --no-input is set: {prompt}",
        )


class NetworkError(CinnaExit):
    """The platform could not be reached (DNS / connect / timeout / TLS)."""

    def __init__(self, target: str, exc: BaseException | str):
        super().__init__(EXIT_NETWORK, "network", f"Could not reach {target}: {exc}")


class SetupTokenError(CinnaExit):
    """The setup-token exchange was refused (invalid / expired / already used)."""

    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(
            EXIT_SETUP_TOKEN,
            "setup_token_invalid",
            f"Account setup failed: {detail}",
            extra={"http_status": status_code} if status_code else None,
        )


class AccountMismatchError(CinnaExit):
    """A refreshed token belongs to a different account than the workspace."""

    def __init__(self, detail: str):
        super().__init__(EXIT_ACCOUNT_MISMATCH, "account_mismatch", detail)


class WorkspaceExistsError(CinnaExit):
    """The target directory already holds a workspace of the requested kind."""

    def __init__(self, detail: str):
        super().__init__(EXIT_ERROR, "workspace_exists", detail)


class ConfigNotFoundError(CinnaExit):
    """No .cinna/config.json found. User needs to run setup."""

    def __init__(self):
        super().__init__(
            EXIT_ERROR,
            "not_a_workspace",
            "Not in a cinna workspace. Run the setup command from the platform UI first.",
        )


class AccountConfigNotFoundError(CinnaExit):
    """No .cinna/account.json found. User needs to run account setup."""

    def __init__(self):
        super().__init__(
            EXIT_ERROR,
            "not_an_account_workspace",
            "Not in a cinna account workspace. Run the account setup command "
            "from the platform UI (Settings → Local Development) first.",
        )


class AuthenticationError(CinnaExit):
    """CLI token rejected by the platform."""

    def __init__(self, detail: str = ""):
        msg = "Authentication failed. Your session may have expired."
        if detail:
            msg += f" ({detail})"
        msg += "\nRun the setup command again from the platform UI."
        super().__init__(EXIT_ERROR, "auth_failed", msg)


class PlatformError(CinnaExit):
    """Backend returned an unexpected error.

    5xx answers map to exit ``12`` (the platform is up but broken — from the
    driver's point of view the same as unreachable: retry later); 4xx keep
    exit ``1`` with the backend detail verbatim.
    """

    def __init__(self, status_code: int, detail: str):
        is_server_error = status_code >= 500
        super().__init__(
            EXIT_NETWORK if is_server_error else EXIT_ERROR,
            "platform_unavailable" if is_server_error else "platform_error",
            f"Platform error ({status_code}): {detail}",
            extra={"http_status": status_code},
        )
        self.status_code = status_code


class MutagenNotFoundError(CinnaExit):
    """Mutagen is not installed or not on PATH."""

    def __init__(self, required_version: str | None = None):
        msg = "Mutagen is required but was not found on PATH."
        if required_version:
            msg += f" (required version: {required_version})"
        msg += "\nInstall with:  brew install mutagen-io/mutagen/mutagen"
        msg += "\nOther platforms: https://mutagen.io/documentation/introduction/installation"
        super().__init__(
            EXIT_ERROR,
            "mutagen_missing",
            msg,
            extra={"required_version": required_version} if required_version else None,
        )


class MutagenVersionMismatchError(CinnaExit):
    """Installed Mutagen version does not match what the platform requires."""

    def __init__(self, installed: str, required: str):
        super().__init__(
            EXIT_ERROR,
            "mutagen_mismatch",
            f"Mutagen version mismatch: installed {installed}, platform requires {required}.\n"
            "Upgrade with:  brew upgrade mutagen-io/mutagen/mutagen",
            extra={"installed_version": installed, "required_version": required},
        )
