"""cinna CLI — local development for Cinna Core agents."""

import logging
import os
import platform
import shlex
import shutil
import sys
import time
from pathlib import Path

import click
import httpx

from cinna import __version__
from cinna import console
from cinna import sync_session
from cinna.client import PlatformClient
from cinna.config import (
    find_workspace_root,
    list_agent_registry,
    load_config,
    remove_agent_registry,
)
from cinna.errors import EXIT_ERROR, CinnaExit, NetworkError
from cinna.mcp_proxy import run_mcp_proxy
from cinna.mutagen_runtime import ensure_mutagen_ready

logger = logging.getLogger("cinna.exec")


class CinnaGroup(click.Group):
    """Root group that maps every failure onto the stable exit-code contract.

    Anything a command raises is normalized to a ``CinnaExit`` here, so the
    process exit code and (in ``--json`` mode) the final error line are
    uniform whether the body raised a ``CinnaExit`` itself, a plain
    ``click.ClickException``, or an ``httpx`` transport error. Human output is
    untouched: a ``ClickException`` still renders as ``Error: …`` on stderr,
    and non-Click crashes keep their traceback outside JSON mode.
    """

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except (CinnaExit, click.exceptions.Exit):
            raise
        except click.Abort:
            if console.json_mode:
                raise CinnaExit(EXIT_ERROR, "aborted", "Aborted.") from None
            raise
        except click.UsageError as exc:
            # Keep Click's usage rendering (and exit code 2) for humans.
            if console.json_mode:
                raise CinnaExit.from_click(exc) from exc
            raise
        except click.ClickException as exc:
            raise CinnaExit.from_click(exc) from exc
        except httpx.TransportError as exc:
            try:
                target = str(exc.request.url)
            except RuntimeError:  # httpx: request not attached to this error
                target = "the platform"
            raise NetworkError(target, exc) from exc
        except Exception as exc:
            if console.json_mode:
                raise CinnaExit(
                    EXIT_ERROR, "internal_error", f"{type(exc).__name__}: {exc}"
                ) from exc
            raise


def _no_input_callback(ctx, param, value):
    if value:
        console.set_no_input(True)


def _json_callback(ctx, param, value):
    if value:
        console.set_json_mode(True)


_NO_INPUT_HELP = (
    "Never prompt: take every default, fail with code 'needs_input' where "
    "there is none (also CINNA_NO_INPUT=1)."
)


def no_input_option(f):
    """``--no-input`` as a per-command option (the root group has it too, but a
    driver that puts it after the subcommand must be honored as well)."""
    return click.option(
        "--no-input",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=_no_input_callback,
        help=_NO_INPUT_HELP,
    )(f)


def json_option(f):
    """``--json``: one JSON object per line on stdout, Rich suppressed, no
    prompts (implies ``--no-input``)."""
    return click.option(
        "--json",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=_json_callback,
        help="Machine-readable output: one JSON object per line on stdout "
        "(progress lines, then a final {\"result\": …} line). Implies --no-input.",
    )(f)


@click.group(cls=CinnaGroup)
@click.version_option(version=__version__)
@click.option("-v", "--verbose", is_flag=True, help="Show debug logs in terminal")
@click.option(
    "--no-input",
    is_flag=True,
    expose_value=False,
    envvar="CINNA_NO_INPUT",
    callback=_no_input_callback,
    help=_NO_INPUT_HELP,
)
def cli(verbose: bool):
    """Local development CLI for Cinna Core agents."""
    from cinna.logging import setup_logging

    setup_logging(verbose=verbose)


# ─── setup ─────────────────────────────────────────────────────────────────


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("setup_input", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option(
    "--name",
    default=None,
    help="Name for this development session",
)
def setup(setup_input: tuple[str, ...], name: str | None):
    """Set up local development environment for an agent.

    Accepts any of these formats (paste directly from the platform UI):

    \b
      cinna setup curl -sL http://host/api/cli-setup/TOKEN | python3 -
      cinna setup http://host/api/cli-setup/TOKEN
      cinna setup TOKEN
    """
    from cinna.bootstrap import run_setup

    name = _resolve_machine_name(name)

    run_setup(" ".join(setup_input), name)


# ─── set-token ─────────────────────────────────────────────────────────────


@cli.command(name="set-token", context_settings={"ignore_unknown_options": True})
@click.argument("setup_input", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option(
    "--name",
    default=None,
    help="Machine name to register with the refreshed token",
)
def set_token(setup_input: tuple[str, ...], name: str | None):
    """Refresh the CLI token on the current workspace.

    Useful when the stored token has expired — swaps ``cli_token`` in
    ``.cinna/config.json`` and ``~/.cinna/agents.json`` in place, without
    re-cloning the workspace or regenerating context files. Must be run from
    inside an existing cinna workspace, and the token must belong to the same
    agent.

    Accepts any of these formats (paste directly from the platform UI):

    \b
      cinna set-token curl -sL http://host/api/cli-setup/TOKEN | python3 -
      cinna set-token http://host/api/cli-setup/TOKEN
      cinna set-token TOKEN
    """
    from cinna.bootstrap import run_set_token

    name = _resolve_machine_name(name)

    run_set_token(" ".join(setup_input), name)


# ─── login ─────────────────────────────────────────────────────────────────


@cli.command()
@click.argument("domain", required=False)
@click.option("--name", default=None, help="Machine name for a new account session")
@click.option(
    "--dir",
    "dir_name",
    default=None,
    help="Subfolder to create the new account workspace in",
)
def login(domain: str | None, name: str | None, dir_name: str | None):
    """Sign in to an account workspace in the browser — resume or connect new.

    Run inside an existing account workspace and it refreshes the stored token
    in place. Run it anywhere else and it connects a *new* account: it asks for
    the platform DOMAIN (or pass it as an argument — protocol optional), then
    creates the workspace in the current folder if empty, or in a subfolder you
    name. Either way it opens an authorization URL; once you click Authorize the
    CLI receives a fresh account token — no setup token to copy/paste.

    \b
      cinna login                       # resume here, or prompt for a domain
      cinna login app.example.com       # connect a new account to that platform
      cinna login app.example.com --dir my-cinna

    Use it when ``cinna account status`` or ``cinna doctor`` reports the account
    token has expired; afterwards ``cinna doctor`` re-mints the dependent
    per-agent tokens.
    """
    from cinna.account import run_login

    run_login(domain=domain, machine_name=name, dir_name=dir_name)


# ─── account group ─────────────────────────────────────────────────────────


@cli.group()
def account():
    """Account-level workspace: discover agents, manage the account session.

    An account workspace is bootstrapped once from the platform's
    Settings → Local Development card. From it, ``cinna agent sync`` attaches
    standard per-agent workspaces under ``agents/`` without any further UI
    interaction.
    """


@account.command(name="setup", context_settings={"ignore_unknown_options": True})
@click.argument("setup_input", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option(
    "--name",
    default=None,
    help="Machine name for this account session",
)
@click.option(
    "--dir",
    "dir_name",
    default=None,
    help="Directory to create the account workspace in. An absolute path is "
    "used as is (parents created); a relative one is under the current "
    "directory (default: the platform domain, e.g. demo-core_opencinna_io; "
    "you'll be prompted to accept or change it)",
)
@no_input_option
@json_option
def account_setup(setup_input: tuple[str, ...], name: str | None, dir_name: str | None):
    """Set up an account workspace from an account setup token.

    Accepts any of these formats (paste directly from Settings → Local
    Development):

    \b
      cinna account setup curl -sL http://host/api/cli-setup/account/TOKEN | python3 -
      cinna account setup http://host/api/cli-setup/account/TOKEN
      cinna account setup TOKEN

    Refuses (exit 1, code ``workspace_exists``) when the target already holds
    ``.cinna/account.json`` — before the single-use token is spent. Exit 10
    means the token was rejected (invalid / expired / used); 12 means the
    platform could not be reached.
    """
    from cinna.account import run_account_setup

    name = _resolve_machine_name(name)

    run_account_setup(" ".join(setup_input), name, dir_name)


@account.command(name="set-token", context_settings={"ignore_unknown_options": True})
@click.argument("setup_input", nargs=-1, type=click.UNPROCESSED, required=True)
@no_input_option
@json_option
def account_set_token(setup_input: tuple[str, ...]):
    """Refresh the account token in place from a new account setup token.

    The account counterpart of ``cinna set-token``: run inside an existing
    account workspace, it re-exchanges a fresh setup token (Settings → Local
    Development, or minted by Cinna Desktop) under the **stored** machine name
    and swaps only ``account_token`` (plus a refreshed platform / frontend URL)
    into ``.cinna/account.json``. The active user workspace, the machine name,
    ``context/`` and every synced child under ``agents/`` are left untouched —
    afterwards ``cinna doctor`` re-mints expired child tokens as usual.

    The exchanged token must belong to the same account as the workspace;
    otherwise nothing is written and the command exits 11
    (``account_mismatch``). Accepts the same inputs as ``cinna account setup``;
    a bare token reuses the stored platform URL.

    \b
      cinna account set-token curl -sL http://host/api/cli-setup/account/TOKEN | python3 -
      cinna account set-token http://host/api/cli-setup/account/TOKEN
      cinna account set-token TOKEN
    """
    from cinna.account import run_account_set_token

    run_account_set_token(" ".join(setup_input))


@account.command(name="agents")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="List agents across all workspaces (default: the active workspace only)",
)
def account_agents(show_all: bool):
    """List the agents this account can access.

    Scoped by default to the **active user workspace** (set with
    ``cinna account user-workspace activate``); pass ``--all`` to list every
    accessible agent across all workspaces. The header states which workspace is
    shown.

    Shows, per agent: name + id, building rights (foreign bundle installs are
    view-only), whether a remote environment is active, and whether a local
    workspace is already synced under ``agents/``.
    """
    from cinna.account import run_account_agents

    run_account_agents(show_all=show_all)


@account.command(name="status")
@no_input_option
@json_option
def account_status():
    """Show account workspace info and account token validity.

    With ``--json`` the summary is a single ``{"result": "ok", …}`` line
    (platform / frontend URLs, machine name, token state, synced agents,
    context-package and cinna-cli version freshness).
    """
    from cinna.account import run_account_status

    run_account_status()


@account.command(name="refresh-context")
def account_refresh_context():
    """Re-download the context package and replace ``context/``.

    The context package (platform docs, generated API reference, example
    scripts) is installed by ``cinna account setup``; refresh it when the
    platform ships updated docs. The old tree is replaced only after a
    successful download.
    """
    from cinna.account import run_account_refresh_context

    run_account_refresh_context()


# ─── account user-workspace group ────────────────────────────────────────────


@account.group(name="user-workspace")
def account_user_workspace():
    """Choose the active user workspace for this account session.

    Workspace-scoped resources created from the account workspace — new agents
    (``cinna agent create``) and the credentials they acquire (``cinna account
    credentials``) — land in the active workspace. The selection is stored
    client-side in ``.cinna/account.json``; the platform keeps no
    active-workspace state.
    """


@account_user_workspace.command(name="list")
def account_user_workspace_list():
    """List the account's workspaces, marking the active one."""
    from cinna.account import run_user_workspace_list

    run_user_workspace_list()


@account_user_workspace.command(name="activate")
@click.argument("workspace_ref")
def account_user_workspace_activate(workspace_ref: str):
    """Set the active workspace to WORKSPACE_REF (name or id).

    Use ``default`` (or ``none``) to clear back to the Default (unassigned)
    workspace.
    """
    from cinna.account import run_user_workspace_activate

    run_user_workspace_activate(workspace_ref)


@account_user_workspace.command(name="clear")
def account_user_workspace_clear():
    """Clear the active workspace back to Default (unassigned)."""
    from cinna.account import run_user_workspace_clear

    run_user_workspace_clear()


# ─── account credentials group ───────────────────────────────────────────────


@account.group(name="credentials")
def account_credentials():
    """Draft and wire credentials for your agents (no secret values).

    The account CLI scaffolds credentials as *drafts* and attaches them to
    agents; it can never read or write a credential's secret value. The user
    fills the secret in the web UI — the draft shows as "needs setup" until then.
    """


@account_credentials.command(name="list")
@click.option(
    "--workspace",
    default=None,
    help="Filter by workspace id ('default' = the Default/unassigned workspace).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON listing.")
def account_credentials_list(workspace: str | None, as_json: bool):
    """List your credentials: name, type, slot, setup status, id (metadata only).

    The SLOT column is the credential's service URI — the id a skill's
    ``credentials:`` block names and a script looks the credential up by.
    Set it with ``cinna account credentials update <id> --service-uri <slot>``.
    """
    from cinna.account import run_credentials_list

    run_credentials_list(workspace, as_json=as_json)


@account_credentials.command(name="types")
def account_credentials_types():
    """List credential types and the fields the user must fill per type."""
    from cinna.account import run_credentials_types

    run_credentials_types()


@account_credentials.command(name="create")
@click.option("--name", required=True, help="Display name for the credential")
@click.option(
    "--type",
    "cred_type",
    required=True,
    help="Credential type (see 'cinna account credentials types'), e.g. api_token",
)
@click.option("--notes", default=None, help="Optional notes for the user")
@click.option("--service-uri", default=None, help="Non-secret audience / target URL")
@click.option("--share", is_flag=True, help="Allow this credential to be shared")
@click.option(
    "--workspace",
    default=None,
    help="Workspace id for the credential (defaults to the active workspace; "
    "'default' = Default/unassigned).",
)
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Also attach the new draft to this agent (name, slug, or id).",
)
def account_credentials_create(
    name: str,
    cred_type: str,
    notes: str | None,
    service_uri: str | None,
    share: bool,
    workspace: str | None,
    agent_ref: str | None,
):
    """Create a draft credential the user completes in the UI.

    The credential is created empty — the CLI never sends a secret value. The
    output lists exactly which fields the user must fill and links to the page
    where they enter them. With ``--agent`` the draft is attached in one step.

    Examples:
      cinna account credentials create --name "Stripe Key" --type api_token
      cinna account credentials create --name "Odoo" --type odoo --agent crm-agent
    """
    from cinna.account import run_credentials_create

    run_credentials_create(
        name, cred_type, notes, service_uri, share, workspace, agent_ref
    )


@account_credentials.command(name="update")
@click.argument("credential_id")
@click.option("--name", default=None, help="New display name")
@click.option("--notes", default=None, help="New notes")
@click.option(
    "--service-uri", default=None, help="New non-secret audience / target URL"
)
@click.option("--share/--no-share", "share", default=None, help="Toggle sharing")
def account_credentials_update(
    credential_id: str,
    name: str | None,
    notes: str | None,
    service_uri: str | None,
    share: bool | None,
):
    """Update a credential's metadata (never its secret value)."""
    from cinna.account import run_credentials_update

    run_credentials_update(credential_id, name, notes, service_uri, share)


@account_credentials.command(name="delete")
@click.argument("credential_id")
@click.option("--force", is_flag=True, help="Override the Tier-2 blast-radius block")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
def account_credentials_delete(credential_id: str, force: bool, yes: bool):
    """Delete a credential (unlinks it from any agents using it)."""
    from cinna.account import run_credentials_delete

    run_credentials_delete(credential_id, force, yes)


@account_credentials.command(name="share-with-agent")
@click.argument("credential_id")
@click.option(
    "--agent",
    "agent_ref",
    required=True,
    help="Agent to attach the credential to (name, slug, or id).",
)
def account_credentials_share_with_agent(credential_id: str, agent_ref: str):
    """Attach an existing credential to an agent you own."""
    from cinna.account import run_credentials_share

    run_credentials_share(credential_id, agent_ref)


# ─── improve group ─────────────────────────────────────────────────────────


@cli.group()
def improve():
    """Improvement requests users shared with you about your agents.

    A user chatting with one of your agents can hand you a frozen snapshot of
    that session — the transcript plus the runtime context that produced it —
    from the session menu or with ``/session-improve``. These verbs are the
    receiving half of that loop: list what came in, read it, download the
    archive for a local coding agent, and close the request with a note the
    requester sees.

    Run from an account workspace: the listing spans every agent you own.
    The shipped playbook is ``context/guides/handling-improvement-requests.md``.
    """


@improve.command(name="list")
@click.option(
    "--status",
    default=None,
    help="Filter by status: new, in_progress, completed, declined.",
)
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Filter to one agent (name, slug, or id).",
)
@click.option("--limit", default=50, show_default=True, help="Max requests to list.")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON listing.")
def improve_list(status: str | None, agent_ref: str | None, limit: int, as_json: bool):
    """List improvement requests across the agents you own.

    Unhandled first, then newest. Start here:

    \b
      cinna improve list --status new
    """
    from cinna.improve import run_improve_list

    run_improve_list(status=status, agent_ref=agent_ref, limit=limit, as_json=as_json)


@improve.command(name="show")
@click.argument("request_ref")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON detail.")
def improve_show(request_ref: str, as_json: bool):
    """Show one request in full, including the runtime context block.

    REQUEST_REF is the short id from ``cinna improve list`` or a full id.
    """
    from cinna.improve import run_improve_show

    run_improve_show(request_ref, as_json=as_json)


@improve.command(name="download")
@click.argument("request_ref")
@click.option(
    "--out",
    "out_dir",
    default=None,
    help="Directory to extract into (default: improvements/<short-id>/).",
)
def improve_download(request_ref: str, out_dir: str | None):
    """Download and extract the session archive for REQUEST_REF.

    Writes README.md, metadata.json, context.json and session/ (transcript in
    Markdown + JSON). Read README.md first — it states what the archive does
    *not* contain.
    """
    from cinna.improve import run_improve_download

    run_improve_download(request_ref, out_dir)


@improve.command(name="status")
@click.argument("request_ref")
@click.argument("new_status")
@click.option(
    "--note",
    default=None,
    help="Resolution note — shown to the person who submitted the request.",
)
def improve_status(request_ref: str, new_status: str, note: str | None):
    """Set the status of REQUEST_REF to NEW_STATUS.

    \b
      cinna improve status a1b2c3d4 in_progress
      cinna improve status a1b2c3d4 completed --note "Fixed in v1.6 — the agent
      no longer re-asks for an uploaded file."
    """
    from cinna.improve import run_improve_status

    run_improve_status(request_ref, new_status, note)


# ─── agent group ───────────────────────────────────────────────────────────


@cli.group()
def agent():
    """Attach / detach per-agent workspaces from the account workspace."""


@agent.command(name="sync")
@click.argument("agent_ref")
@click.option(
    "--name",
    default=None,
    help="Machine name for the minted token (defaults to the account machine name)",
)
def agent_sync(agent_ref: str, name: str | None):
    """Mint a CLI token for AGENT_REF and attach a standard workspace.

    AGENT_REF is the agent's display name, slug, or id (see
    ``cinna account agents``). The workspace lands under
    ``agents/<slug>/`` and is identical to one created by ``cinna setup`` —
    ``cd agents/<slug> && cinna dev`` afterwards.
    """
    from cinna.account import run_agent_sync

    run_agent_sync(agent_ref, name)


@agent.command(name="unsync")
@click.argument("agent_ref")
def agent_unsync(agent_ref: str):
    """Detach AGENT_REF's workspace: stop sync, revoke its token, clean up.

    Equivalent to ``cinna disconnect`` inside ``agents/<slug>/`` plus a
    server-side revoke of the minted token. Workspace files are preserved.
    """
    from cinna.account import run_agent_unsync

    run_agent_unsync(agent_ref)


@agent.command(name="create")
@click.argument("name")
@click.option("--description", default=None, help="Agent description")
def agent_create(name: str, description: str | None):
    """Create a new agent on the platform (run from the account workspace).

    Thin client: only NAME (and the optional description) is sent — the
    backend applies all defaults (AI credentials, env template, environment)
    exactly as creating from the UI does. Prints the created agent's id and
    web UI link; attach a local workspace afterwards with
    ``cinna agent sync <name>``.
    """
    from cinna.account import run_agent_create

    run_agent_create(name, description)


@agent.command(name="import")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--name", default=None, help="Override the manifest's agent name")
@click.option(
    "--workspace",
    default=None,
    help="Target user workspace (name or id; 'default' for the Default one)",
)
@click.option(
    "--update",
    is_flag=True,
    help="Re-import into the agent this manifest already points at",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the plan (files, credentials, schedules) without any platform write",
)
@click.option("--no-push", is_flag=True, help="Copy the files but skip 'cinna sync push'")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
def agent_import(
    path: str,
    name: str | None,
    workspace: str | None,
    update: bool,
    dry_run: bool,
    no_push: bool,
    yes: bool,
):
    """Import an agent built locally with the Local Agent Kit.

    PATH is the local agent folder — the one holding ``cinna-agent.json``
    (typically ``../Local/<slug>`` next to this account workspace). Run from
    the account workspace root or any folder inside it.

    Nine idempotent steps: read the manifest, create (or resolve) the cloud
    agent, write its prompts and metadata, attach a local workspace, copy the
    tree honouring the contract's exclude list and secret rules, push it,
    create the credential drafts (printing the URLs the user opens to fill
    them), create the schedules, and record the publication in
    ``publications.json`` beside the manifest.

    ``credentials/``, ``app-data/`` and every dotenv shape are never copied and
    no secret value is ever read or printed. A partial import is resumed with
    ``--update``, which resolves the agent by the ledger entry whose
    ``platform_url`` matches the instance you are logged into.
    """
    from cinna.local_import import run_agent_import

    run_agent_import(
        path,
        name=name,
        workspace=workspace,
        update=update,
        dry_run=dry_run,
        no_push=no_push,
        yes=yes,
    )


@agent.command(name="restart-env")
@click.argument("agent_ref")
def agent_restart_env(agent_ref: str):
    """Restart AGENT_REF's environment (recover a stuck env / poisoned API).

    The first-class recovery path: bounces the agent's container without the
    raw API escape hatch. Blocks until the env is back, then prints its status.
    Use this when a producer's REST API is stuck reporting an old error, or the
    env is otherwise wedged.

    A restart re-runs the same image: it does NOT update the container's core
    or SDK helpers. An environment missing a newer feature (e.g. the
    credential slot helpers) needs 'cinna agent rebuild-env'.
    """
    from cinna.account import run_agent_restart_env

    run_agent_restart_env(agent_ref)


@agent.command(name="rebuild-env")
@click.argument("agent_ref")
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    help=(
        "Skip the 'this takes a few minutes' confirmation. Does NOT skip the "
        "unsynced-local-changes prompt, which still aborts on a dirty "
        "workspace — run 'cinna sync push --agent <name>' first."
    ),
)
def agent_rebuild_env(agent_ref: str, yes: bool):
    """Rebuild AGENT_REF's environment (pick up new platform features).

    Heavier than 'restart-env' and not the same thing: a restart re-runs the
    same image, a rebuild replaces the container's core from the template. Use
    this when an environment built before a feature existed cannot answer for
    it — e.g. a skills refresh that keeps reporting 'adapter_unsupported'.
    Takes a few minutes and blocks until it is done.
    """
    from cinna.account import run_agent_rebuild_env

    run_agent_rebuild_env(agent_ref, yes=yes)


@agent.command(name="show")
@click.argument("agent_ref")
@click.option(
    "--prompts",
    "prompts_only",
    is_flag=True,
    help="Show only the effective prompts (skip features + credentials)",
)
@click.option(
    "--full",
    "full",
    is_flag=True,
    help="Print prompts in full (default truncates long prompts for display)",
)
def agent_show(agent_ref: str, prompts_only: bool, full: bool):
    """Show AGENT_REF's effective prompts, features, and connected credentials.

    Prints the prompts the runtime actually reads, the enabled features, and
    the names/types of connected credentials (never secrets) — so you can
    confirm "is what I edited actually live?" without opening the browser.

    Long prompts are truncated for readability; pass --full to print them
    in their entirety (e.g. when redirecting to a file).
    """
    from cinna.account import run_agent_show

    run_agent_show(agent_ref, prompts_only, full)


# ─── agent prompts subgroup ────────────────────────────────────────────────


_PROMPTS_DIR_HELP = (
    "Folder holding the prompt files (default: prompts/<agent-slug>/ at the "
    "account root)."
)


@agent.group(name="prompts")
def agent_prompts():
    """Edit an agent's prompts as local files: pull, diff, push.

    \b
    pull   writes workflow.md, entrypoint.md, refiner.md, router_trigger.md,
           description.md and example_prompts.json, and records what it pulled
    diff   shows local edits against the platform, and flags a field the
           platform changed since the pull
    push   sends only the fields you edited, then pushes the doc prompts into
           the running environment

    The agent's config (the database) is authoritative. The environment's
    docs/*.md mirror three of these prompts; editing those synced files as
    well as these is a last-writer-wins race — pick one path.
    """


@agent_prompts.command(name="pull")
@click.argument("agent_ref")
@click.option("--dir", "dir_opt", default=None, help=_PROMPTS_DIR_HELP)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite the folder even if it holds unpushed edits or another agent's prompts.",
)
def agent_prompts_pull(agent_ref: str, dir_opt: str | None, force: bool):
    """Write AGENT_REF's prompts, description and examples to files."""
    from cinna.account import run_agent_prompts_pull

    run_agent_prompts_pull(agent_ref, dir_opt, force)


@agent_prompts.command(name="diff")
@click.argument("agent_ref")
@click.option("--dir", "dir_opt", default=None, help=_PROMPTS_DIR_HELP)
def agent_prompts_diff(agent_ref: str, dir_opt: str | None):
    """Show how the local prompt files differ from AGENT_REF's config."""
    from cinna.account import run_agent_prompts_diff

    run_agent_prompts_diff(agent_ref, dir_opt)


@agent_prompts.command(name="push")
@click.argument("agent_ref")
@click.option("--dir", "dir_opt", default=None, help=_PROMPTS_DIR_HELP)
@click.option(
    "--sync-env/--no-sync-env",
    "sync_env",
    default=True,
    show_default=True,
    help="After the write, push workflow/entrypoint/refiner into the running "
    "environment's docs/*.md (without it they arrive on its next start).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Push a field even if the platform changed it since the pull.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be pushed; write nothing.")
def agent_prompts_push(
    agent_ref: str, dir_opt: str | None, sync_env: bool, force: bool, dry_run: bool
):
    """Write the edited prompt files back to AGENT_REF in one bulk write.

    Only fields whose file changed since the pull are sent. A field the
    platform changed since the pull is left alone when you did not edit it,
    and refused when you did (unless --force) — so a stale file never silently
    reverts someone else's change. Delete a file to leave its field untouched.
    """
    from cinna.account import run_agent_prompts_push

    run_agent_prompts_push(agent_ref, dir_opt, sync_env, force, dry_run)


# ─── agent model subgroup ──────────────────────────────────────────────────


_MODEL_VALUE_HELP = "a model id, or 'default' to clear the override (the catalog default)"


@agent.group(name="model")
def agent_model():
    """Show or set the model an agent runs in each mode.

    \b
    show   both modes' SDK, override, effective model and health
    set    change a mode's model or AI credential, then rebuild

    The model is a setting of the agent's environment, not of the agent:
    'cinna api PUT agents/<id>' cannot change it. 'set' copies both modes'
    current settings, changes only what you pass, and rebuilds the environment
    so the change applies. Model ids are not checked locally — the platform's
    catalog decides, and 'show' reports its verdict under health.
    """


@agent_model.command(name="show")
@click.argument("agent_ref")
@click.option("--json", "as_json", is_flag=True, help="Print the settings as JSON.")
def agent_model_show(agent_ref: str, as_json: bool):
    """Show AGENT_REF's SDK, model override, effective model and health per mode."""
    from cinna.account import run_agent_model_show

    run_agent_model_show(agent_ref, as_json)


@agent_model.command(name="set")
@click.argument("agent_ref")
@click.option(
    "--conversation",
    default=None,
    metavar="MODEL|default",
    help=f"Conversation-mode model: {_MODEL_VALUE_HELP}.",
)
@click.option(
    "--building",
    default=None,
    metavar="MODEL|default",
    help=f"Building-mode model: {_MODEL_VALUE_HELP}.",
)
@click.option(
    "--conversation-credential",
    default=None,
    metavar="ID|default",
    help="Pin conversation mode to this AI credential id, or 'default' to unpin it.",
)
@click.option(
    "--building-credential",
    default=None,
    metavar="ID|default",
    help="Pin building mode to this AI credential id, or 'default' to unpin it. "
    "Both credentials 'default' switches back to the account's default AI credentials.",
)
@click.option(
    "--no-rebuild",
    "no_rebuild",
    is_flag=True,
    help="Store the change without rebuilding; it applies on the next "
    "'cinna agent rebuild-env'.",
)
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    help="Skip the 'this rebuilds the environment' confirmation. Does NOT skip "
    "the unsynced-local-changes prompt.",
)
def agent_model_set(
    agent_ref: str,
    conversation: str | None,
    building: str | None,
    conversation_credential: str | None,
    building_credential: str | None,
    no_rebuild: bool,
    yes: bool,
):
    """Change AGENT_REF's model or AI credential for one or both modes.

    Only what you pass changes: the other mode and the credential pins keep
    their current values, and a setting that is already in place is a no-op.
    By default the environment is then rebuilt (minutes, blocking) and the
    command waits until it answers its health check.

    \b
      cinna agent model set crm-agent --conversation haiku
      cinna agent model set crm-agent --building default --no-rebuild
    """
    from cinna.account import run_agent_model_set

    run_agent_model_set(
        agent_ref,
        conversation=conversation,
        building=building,
        conversation_credential=conversation_credential,
        building_credential=building_credential,
        rebuild=not no_rebuild,
        yes=yes,
    )


# ─── agent scenarios subgroup ──────────────────────────────────────────────


_SCENARIOS_PATH_HELP = (
    "Scenario folder (default: the synced workspace's docs/test_scenarios/). "
    "An agent folder that holds docs/test_scenarios/ works too, e.g. a Local "
    "Agent Kit agent's Local/<slug>."
)


@agent.group(name="scenarios")
def agent_scenarios():
    """Re-run an agent's recorded test scenarios (docs/test_scenarios/*.md).

    \b
    list   every scenario file and how many Say | Expect rows it holds
    run    send every Say row to the agent, each in a fresh session

    Each file's '## Say | Expect' table is the contract (CHAT_TESTING.md,
    "Record the conditions"); README.md is not a scenario file. The files are
    read locally and never pushed. 'run' does not judge pass or fail: Expect is
    prose, so the report is for you to mark.
    """


@agent_scenarios.command(name="list")
@click.argument("agent_ref")
@click.option(
    "--path", "path_opt", default=None, type=click.Path(file_okay=False), help=_SCENARIOS_PATH_HELP
)
def agent_scenarios_list(agent_ref: str, path_opt: str | None):
    """List AGENT_REF's scenario files and their row counts."""
    from cinna.scenarios import run_scenarios_list

    run_scenarios_list(agent_ref, path_opt)


@agent_scenarios.command(name="run")
@click.argument("agent_ref")
@click.argument("files", nargs=-1)
@click.option(
    "--path", "path_opt", default=None, type=click.Path(file_okay=False), help=_SCENARIOS_PATH_HELP
)
@click.option(
    "--timeout",
    type=int,
    default=600,
    show_default=True,
    help="Max seconds to wait for each case's turn.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="One JSON object per case, then a summary. Needs --yes."
)
@click.option(
    "--out",
    "out",
    default=None,
    type=click.Path(dir_okay=False),
    help="Also write a Markdown report to this file.",
)
@click.option("--yes", is_flag=True, help="Skip the 'each case is a real agent turn' confirmation.")
def agent_scenarios_run(
    agent_ref: str,
    files: tuple[str, ...],
    path_opt: str | None,
    timeout: int,
    as_json: bool,
    out: str | None,
    yes: bool,
):
    """Send every Say row of AGENT_REF's scenarios and report what came back.

    FILES narrows the run to some scenario files (name, with or without .md).
    Rows run one after another, each in a fresh conversation session, so no
    row leaks context into a later one. Per case: the reply, the tool calls,
    the outcome (completed, timeout, not_started) and the session id. A case
    that does not complete is never re-sent — its 'cinna chat --attach'
    command is printed — and the command exits non-zero at the end.

    \b
      cinna agent scenarios run crm-agent
      cinna agent scenarios run crm-agent scope_and_pushback --out run.md
    """
    from cinna.scenarios import run_scenarios_run

    run_scenarios_run(
        agent_ref,
        files,
        path_opt=path_opt,
        timeout=timeout,
        as_json=as_json,
        out=out,
        yes=yes,
    )


# ─── agent schedule subgroup ───────────────────────────────────────────────


@agent.group(name="schedule")
def agent_schedule():
    """Manage AGENT_REF's automatic-execution schedules (CRON).

    Full CRUD over an agent's schedules — the CLI equivalent of the agent
    Config → Schedules card. A schedule is either a ``static_prompt`` (always
    starts a session) or a ``script_trigger`` (runs a command, only starts a
    session when the output is not "OK"). On a foreign (bundle) install the
    definitions are publisher-managed — you can toggle / run / view logs only.
    Run from the account workspace.
    """


@agent_schedule.command(name="list")
@click.argument("agent_ref")
def agent_schedule_list(agent_ref: str):
    """List AGENT_REF's schedules."""
    from cinna.account import run_schedule_list

    run_schedule_list(agent_ref)


@agent_schedule.command(name="generate")
@click.argument("agent_ref")
@click.argument("text")
@click.option(
    "--tz",
    "timezone",
    default="UTC",
    help="IANA timezone for interpretation (default UTC)",
)
@click.option(
    "--type",
    "schedule_type",
    type=click.Choice(["static_prompt", "script_trigger"]),
    default="static_prompt",
    help="Which minimum-interval floor applies to the generated cadence",
)
def agent_schedule_generate(
    agent_ref: str, text: str, timezone: str, schedule_type: str
):
    """Preview a CRON string from natural-language TEXT (nothing is saved).

    Example: cinna agent schedule generate crm-agent "every weekday at 7am" --tz Europe/Berlin
    """
    from cinna.account import run_schedule_generate

    run_schedule_generate(agent_ref, text, timezone, schedule_type)


@agent_schedule.command(name="create")
@click.argument("agent_ref")
@click.option("--name", required=True, help="Schedule name")
@click.option(
    "--cron", required=True, help="CRON expression in --tz local time (5 fields)"
)
@click.option(
    "--tz", "timezone", default="UTC", help="IANA timezone for the cron (default UTC)"
)
@click.option(
    "--type",
    "schedule_type",
    type=click.Choice(["static_prompt", "script_trigger"]),
    default="static_prompt",
    help="static_prompt (always starts a session) or script_trigger (runs a command)",
)
@click.option("--prompt", default=None, help="Per-schedule prompt (static_prompt only)")
@click.option(
    "--command", default=None, help="Shell command (required for script_trigger)"
)
@click.option(
    "--description", default=None, help="Human description (defaults to the name)"
)
@click.option("--disabled", is_flag=True, help="Create the schedule disabled")
def agent_schedule_create(
    agent_ref: str,
    name: str,
    cron: str,
    timezone: str,
    schedule_type: str,
    prompt: str | None,
    command: str | None,
    description: str | None,
    disabled: bool,
):
    """Create a schedule on AGENT_REF.

    Examples:
      cinna agent schedule create crm-agent --name "Daily report" \\
        --cron "0 7 * * 1-5" --tz Europe/Berlin --prompt "Produce the daily report"
      cinna agent schedule create crm-agent --name "DB check" \\
        --cron "*/30 * * * *" --tz UTC --type script_trigger \\
        --command "python scripts/check_db.py"
    """
    from cinna.account import run_schedule_create

    run_schedule_create(
        agent_ref,
        name,
        cron,
        timezone,
        schedule_type,
        prompt,
        command,
        description,
        enabled=not disabled,
    )


@agent_schedule.command(name="update")
@click.argument("agent_ref")
@click.argument("schedule_id")
@click.option(
    "--enable/--disable", "enabled", default=None, help="Enable or disable the schedule"
)
@click.option("--name", default=None, help="New name")
@click.option("--cron", default=None, help="New CRON expression (requires --tz)")
@click.option(
    "--tz",
    "timezone",
    default=None,
    help="IANA timezone (required when --cron changes)",
)
@click.option("--prompt", default=None, help="New per-schedule prompt")
@click.option("--command", default=None, help="New shell command (script_trigger)")
@click.option("--description", default=None, help="New description")
def agent_schedule_update(
    agent_ref: str,
    schedule_id: str,
    enabled: bool | None,
    name: str | None,
    cron: str | None,
    timezone: str | None,
    prompt: str | None,
    command: str | None,
    description: str | None,
):
    """Update / toggle SCHEDULE_ID on AGENT_REF.

    Only the fields you pass are changed. On a foreign (bundle) install only
    --enable/--disable is permitted (the definition is publisher-managed).
    """
    from cinna.account import run_schedule_update

    run_schedule_update(
        agent_ref,
        schedule_id,
        enabled,
        name,
        cron,
        timezone,
        prompt,
        command,
        description,
    )


@agent_schedule.command(name="run")
@click.argument("agent_ref")
@click.argument("schedule_id")
def agent_schedule_run(agent_ref: str, schedule_id: str):
    """Trigger SCHEDULE_ID immediately (Run now)."""
    from cinna.account import run_schedule_run

    run_schedule_run(agent_ref, schedule_id)


@agent_schedule.command(name="logs")
@click.argument("agent_ref")
@click.argument("schedule_id")
def agent_schedule_logs(agent_ref: str, schedule_id: str):
    """Show SCHEDULE_ID's last 50 execution logs."""
    from cinna.account import run_schedule_logs

    run_schedule_logs(agent_ref, schedule_id)


@agent_schedule.command(name="delete")
@click.argument("agent_ref")
@click.argument("schedule_id")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
def agent_schedule_delete(agent_ref: str, schedule_id: str, yes: bool):
    """Delete SCHEDULE_ID from AGENT_REF (403 on a foreign install)."""
    from cinna.account import run_schedule_delete

    run_schedule_delete(agent_ref, schedule_id, yes)


# ─── agent status subgroup ─────────────────────────────────────────────────


@agent.group(name="status")
def agent_status():
    """Inspect AGENT_REF's self-reported status and refresh command.

    The CLI equivalent of the Integrations → Agent status card: read the
    agent's STATUS.md snapshot, force a live re-read, and configure the
    pre-command that regenerates it. Run from the account workspace.
    """


@agent_status.command(name="show")
@click.argument("agent_ref")
def agent_status_show(agent_ref: str):
    """Show AGENT_REF's cached status snapshot + configured refresh command."""
    from cinna.account import run_status_show

    run_status_show(agent_ref, force_refresh=False)


@agent_status.command(name="refresh")
@click.argument("agent_ref")
def agent_status_refresh(agent_ref: str):
    """Force a live STATUS.md re-read (wakes a suspended env; never fails)."""
    from cinna.account import run_status_show

    run_status_show(agent_ref, force_refresh=True)


@agent_status.command(name="set-command")
@click.argument("agent_ref")
@click.argument("command")
def agent_status_set_command(agent_ref: str, command: str):
    """Set AGENT_REF's status-refresh pre-command.

    COMMAND is a raw shell/Python string or a ``/run:<name>`` reference. Pass
    an empty string ("") to opt out of running any pre-command. The platform
    default is ``/run:status``.
    """
    from cinna.account import run_status_set_command

    run_status_set_command(agent_ref, command)


# ─── connect group ─────────────────────────────────────────────────────────


@cli.group()
def connect():
    """Wire agents together from the account workspace.

    One-click producer→consumer connections: the producer's REST API
    (``agent-api``) or its agent2agent MCP connector (``mcp``). Agents are
    referenced by display name, slug, or id (see ``cinna account agents``).
    """


@connect.command(name="agent-api")
@click.option(
    "--producer",
    "producer_ref",
    required=True,
    help="Agent exposing the REST API (name, slug, or id)",
)
@click.option(
    "--consumer",
    "consumer_ref",
    required=True,
    help="Agent that will call it (name, slug, or id)",
)
@click.option("--label", default=None, help="Label for the created credential")
@click.option(
    "--read-only", is_flag=True, help="Restrict the consumer to read-only API access"
)
def connect_agent_api(
    producer_ref: str, consumer_ref: str, label: str | None, read_only: bool
):
    """Connect CONSUMER to PRODUCER's REST API.

    Mints a producer API token and attaches it to the consumer as a
    credential. The credential rides the consumer's normal credential sync
    into its remote environment — no manual key handling.
    """
    from cinna.account import run_connect_agent_api

    run_connect_agent_api(producer_ref, consumer_ref, label, read_only)


@connect.command(name="mcp")
@click.option(
    "--producer",
    "producer_ref",
    required=True,
    help="Agent exposing an agent2agent MCP connector (name, slug, or id)",
)
@click.option(
    "--consumer",
    "consumer_ref",
    required=True,
    help="Agent that will consume it (name, slug, or id)",
)
@click.option("--label", default=None, help="Label for the created credential")
@click.option(
    "--conversation-only",
    is_flag=True,
    help="Enable the connection in conversation mode only",
)
@click.option(
    "--building-only", is_flag=True, help="Enable the connection in building mode only"
)
def connect_mcp(
    producer_ref: str,
    consumer_ref: str,
    label: str | None,
    conversation_only: bool,
    building_only: bool,
):
    """Connect CONSUMER to PRODUCER's agent2agent MCP connector.

    The producer is resolved against the discoverable-connectors listing
    (it must expose an agent2agent MCP connector your account may consume).
    By default the connection is enabled in both conversation and building
    modes. If the connector requires OAuth, the printed authorize URL must
    be opened to finish the connection.
    """
    if conversation_only and building_only:
        raise click.ClickException(
            "--conversation-only and --building-only are mutually exclusive."
        )

    from cinna.account import run_connect_mcp

    run_connect_mcp(producer_ref, consumer_ref, label, conversation_only, building_only)


# ─── agent-api group ───────────────────────────────────────────────────────


@cli.group(name="agent-api")
def agent_api():
    """Manage a producer agent's REST API (the `agent_api` feature).

    The build→verify loop a coding agent drives before wiring two agents:
    ``enable`` the API on a producer, author ``agent_api/*.py`` + ``policy.yaml``
    in its synced workspace, ``refresh`` to re-harvest the OpenAPI spec, and
    ``spec`` to read it back. Once verified, wire a consumer with
    ``cinna connect agent-api``. Run from the account workspace.
    """


@agent_api.command(name="enable")
@click.argument("agent_ref")
@click.option(
    "--disable", is_flag=True, help="Disable the REST API instead of enabling it"
)
def agent_api_enable(agent_ref: str, disable: bool):
    """Enable (or --disable) the REST API on producer AGENT_REF.

    AGENT_REF is the agent's display name, slug, or id (see
    ``cinna account agents``). Prints the resulting status so you can confirm
    the toggle and whether a spec is already available.
    """
    from cinna.account import run_agent_api_enable

    run_agent_api_enable(agent_ref, enabled=not disable)


@agent_api.command(name="refresh")
@click.argument("agent_ref")
def agent_api_refresh(agent_ref: str):
    """Re-harvest AGENT_REF's OpenAPI spec + policy.yaml on demand.

    Use after editing the producer's ``agent_api/`` code or ``policy.yaml`` so
    the cached spec/guardrails pick up the change without waiting for the next
    automatic reload. Prints the status (including any harvest error).
    """
    from cinna.account import run_agent_api_refresh

    run_agent_api_refresh(agent_ref)


@agent_api.command(name="spec")
@click.argument("agent_ref")
@click.option(
    "--output",
    "-o",
    default=None,
    help="Write the spec JSON to this file instead of stdout",
)
def agent_api_spec(agent_ref: str, output: str | None):
    """Print AGENT_REF's harvested OpenAPI spec as JSON (or save with -o).

    Reads the cached spec (or harvests import-only from a running env). Plain
    JSON to stdout so it pipes / parses cleanly.
    """
    from cinna.account import run_agent_api_spec

    run_agent_api_spec(agent_ref, output)


@agent_api.command(name="call")
@click.argument("agent_ref")
@click.argument("path")
@click.option(
    "--method",
    "-X",
    default="GET",
    type=click.Choice(
        ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        case_sensitive=False,
    ),
    help="HTTP method (default GET)",
)
@click.option(
    "--query", "query_pairs", multiple=True, help="Query param key=value (repeatable)"
)
@click.option("--json", "json_text", default=None, help="Inline JSON request body")
def agent_api_call(
    agent_ref: str,
    path: str,
    method: str,
    query_pairs: tuple[str, ...],
    json_text: str | None,
):
    """Smoke-test AGENT_REF's own REST API endpoint at PATH.

    Calls the producer's endpoint through the owner-preview proxy (no consumer
    token, no policy edge). Query params ARE forwarded, so this verifies an
    endpoint end-to-end — including query handling — in one shot, instead of
    hand-rolling a consumer probe. Exit code is 0 for a 2xx, 1 for a 4xx/5xx
    (the body is printed either way).

    Examples:
      cinna agent-api call btc-rate-api btc-rate --query vs_currency=eur
      cinna agent-api call orders-api orders -X POST --json '{"sku": "A1"}'
    """
    from cinna.account import run_agent_api_call

    run_agent_api_call(agent_ref, method, path, query_pairs, json_text)


# ─── skills group ──────────────────────────────────────────────────────────


@cli.group(name="skills")
def skills():
    """The skills lifecycle: what an agent carries, and what the catalog holds.

    A **skill** is a ``skills/<name>/`` folder with a ``SKILL.md`` the engine
    loads on demand; an **addon** is that or an installed plugin. ``list``
    shows both halves as the platform deduplicates them (a catalog install
    appears once, as a skill).

    \b
    On one agent:   list · install · uninstall · update · toggle · refresh
    On the catalog: catalog · show · revisions · files · publish
    On sharing:     grants · grant · revoke · visibility · delist · relist

    Every verb takes the references a person has rather than the ids the API
    wants: an agent by name, slug or id, a package by its reverse-DNS id or
    name, an installed skill by the name ``list`` prints. Run from the account
    workspace.
    """


@skills.command(name="list")
@click.argument("agent_ref")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON listing.")
def skills_list(agent_ref: str, as_json: bool):
    """List AGENT_REF's addons — plugins and skills in one deduplicated list.

    AGENT_REF is the agent's display name, slug, or id (see
    ``cinna account agents``). Prints kind / source / status / name / version
    per addon — the version as the skill's own SKILL.md reports it — and marks
    the local skills already published to the catalog. Reads the server's
    cache, so it never wakes a sleeping environment: a stale or unreadable
    skill index is reported instead of hidden, and a version it has not
    backfilled yet is simply blank.
    """
    from cinna.account import run_skills_list

    run_skills_list(agent_ref, as_json=as_json)


@skills.command(name="publish")
@click.argument("agent_ref")
@click.argument("name")
@click.option(
    "--visibility",
    type=click.Choice(["public", "private", "users"]),
    default=None,
    help="Who may see the package (default: private on a first publish; "
    "unchanged on a re-publish)",
)
@click.option(
    "--grant",
    "grant_emails",
    multiple=True,
    metavar="EMAIL",
    help="Grant catalog access to EMAIL (repeatable, additive, never revokes). "
    "Requires --visibility users.",
)
@click.option(
    "--version",
    default=None,
    help="Override the derived version (one line, max 64 chars). Normally "
    "omit: the platform continues the series in the skill's own SKILL.md.",
)
@click.option("--notes", "release_notes", default=None, help="Release notes")
@click.option(
    "--package-id",
    default=None,
    help="Reverse-DNS package id, first publish only (e.g. com.acme.pdf-report). "
    "Derived from the skill name when omitted.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the version, package id and revision a publish would take, "
    "and publish nothing.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_publish(
    agent_ref: str,
    name: str,
    visibility: str | None,
    grant_emails: tuple[str, ...],
    version: str | None,
    release_notes: str | None,
    package_id: str | None,
    dry_run: bool,
    yes: bool,
    as_json: bool,
):
    """Publish AGENT_REF's skill NAME to the instance skills catalog.

    NAME is the skill's folder name as ``cinna skills list`` prints it. A
    re-publish appends a revision to the same package. Without a visibility the
    package is private and nobody else sees it; ``--grant`` names people on a
    ``users`` package and never takes access away (revoke is its own verb in the
    web UI), and it *requires* ``--visibility users`` because no other
    visibility consults the grant list.

    Publishing reads the agent's **cloud** workspace, so ``cinna sync push``
    first — an unsynced edit is left out of a revision you cannot rewrite.

    The **version is derived**, not typed: the platform reads ``version:`` from
    the skill's own ``SKILL.md``, continues the series past the newest release
    (``1.0.0`` for a skill nobody has versioned), and writes the result back
    into that file. ``--version`` overrides it verbatim for one revision, and
    ``--dry-run`` shows what would be taken. At a terminal the same preview is
    shown for confirmation before the press; ``--yes``, ``--json`` and
    ``--no-input`` publish straight away.

    \b
      cinna skills publish crm-agent pdf-report --visibility public
      cinna skills publish crm-agent pdf-report --dry-run
      cinna skills publish crm-agent pdf-report --visibility users \\
          --grant alice@example.com --grant bob@example.com
    """
    from cinna.account import run_skills_publish

    run_skills_publish(
        agent_ref,
        name,
        visibility,
        grant_emails,
        version,
        release_notes,
        package_id,
        dry_run=dry_run,
        yes=yes,
        as_json=as_json,
    )


@skills.command(name="install")
@click.argument("agent_ref")
@click.argument("package_ref", metavar="PACKAGE")
@click.option(
    "--revision",
    type=int,
    default=None,
    help="Install this revision number (default: the newest release).",
)
@click.option(
    "--conversation-only",
    is_flag=True,
    help="Offer the skill in conversation mode only.",
)
@click.option(
    "--building-only", is_flag=True, help="Offer the skill in building mode only."
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_install(
    agent_ref: str,
    package_ref: str,
    revision: int | None,
    conversation_only: bool,
    building_only: bool,
    as_json: bool,
):
    """Install catalog package PACKAGE onto AGENT_REF.

    PACKAGE is the reverse-DNS package id ``cinna skills catalog`` prints
    (``com.acme.pdf-report``), a package's display name, or its UUID. AGENT_REF
    is a name, slug or id, as everywhere else.

    A revision is immutable, so an install pins bytes that will not change
    under the agent; omitting ``--revision`` takes whatever the catalog calls
    latest *now*, and `cinna skills update` is how it moves later. Both modes
    are on unless one of the ``--*-only`` flags narrows it.

    \b
      cinna skills install crm-agent com.acme.pdf-report
      cinna skills install crm-agent pdf-report --revision 2 --conversation-only
    """
    from cinna.account import run_skills_install

    run_skills_install(
        agent_ref,
        package_ref,
        revision=revision,
        conversation_only=conversation_only,
        building_only=building_only,
        as_json=as_json,
    )


@skills.command(name="uninstall")
@click.argument("agent_ref")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_uninstall(agent_ref: str, name: str, yes: bool, as_json: bool):
    """Remove installed skill NAME from AGENT_REF.

    NAME is the name ``cinna skills list`` prints. Only an *install* can be
    uninstalled: an agent's own ``skills/<name>/`` folder is part of its
    workspace, and the command says so rather than failing on a route that was
    never going to match.

    The package and its revisions are untouched — this removes one agent's
    copy, not the thing that was published. Asks first; ``--yes`` skips that,
    and ``--json`` requires ``--yes`` rather than assuming it.
    """
    from cinna.account import run_skills_uninstall

    run_skills_uninstall(agent_ref, name, yes=yes, as_json=as_json)


@skills.command(name="update")
@click.argument("agent_ref")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_update(agent_ref: str, name: str, as_json: bool):
    """Move AGENT_REF's installed skill NAME to the newest revision.

    The version an agent carries and the version the catalog holds are two
    different facts; ``cinna skills list`` marks a row where they differ, and
    this is the verb that closes the gap.
    """
    from cinna.account import run_skills_update

    run_skills_update(agent_ref, name, as_json=as_json)


@skills.command(name="toggle")
@click.argument("agent_ref")
@click.argument("name")
@click.option(
    "--enable/--disable",
    "enable",
    default=None,
    help="Turn the installed skill on or off for this agent.",
)
@click.option(
    "--conversation-mode/--no-conversation-mode",
    "conversation_mode",
    default=None,
    help="Offer (or stop offering) the skill in conversation mode.",
)
@click.option(
    "--building-mode/--no-building-mode",
    "building_mode",
    default=None,
    help="Offer (or stop offering) the skill in building mode.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_toggle(
    agent_ref: str,
    name: str,
    enable: bool | None,
    conversation_mode: bool | None,
    building_mode: bool | None,
    as_json: bool,
):
    """Enable, disable, or re-scope AGENT_REF's installed skill NAME.

    Every switch defaults to "leave it alone": the update is partial, so
    ``--disable`` cannot silently reset the two mode flags a previous call set.

    \b
      cinna skills toggle crm-agent pdf-report --disable
      cinna skills toggle crm-agent pdf-report --no-building-mode
    """
    from cinna.account import run_skills_toggle

    run_skills_toggle(
        agent_ref,
        name,
        enable=enable,
        conversation_mode=conversation_mode,
        building_mode=building_mode,
        as_json=as_json,
    )


@skills.command(name="refresh")
@click.argument("agent_ref")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_refresh(agent_ref: str, as_json: bool):
    """Rebuild AGENT_REF's addon index.

    ``cinna skills list`` reads a server-side cache so it never wakes a
    sleeping environment. This is the other half of that bargain: the verb to
    run when the list reports an unreadable index, or shows a skill without the
    version its SKILL.md carries.
    """
    from cinna.account import run_skills_refresh

    run_skills_refresh(agent_ref, as_json=as_json)


@skills.command(name="catalog")
@click.option("--search", default=None, metavar="Q", help="Filter by name or id.")
@click.option("--mine", is_flag=True, help="Only packages this account published.")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON listing.")
def skills_catalog(search: str | None, mine: bool, as_json: bool):
    """Browse the instance skills catalog.

    Prints the reverse-DNS package id, display name, visibility and newest
    version of every package this account may see — the id being the thing
    `cinna skills install` takes.
    """
    from cinna.account import run_skills_catalog

    run_skills_catalog(search=search, mine=mine, as_json=as_json)


@skills.command(name="show")
@click.argument("package_ref", metavar="PACKAGE")
@click.option(
    "--revision",
    type=int,
    default=None,
    help="Show this revision's SKILL.md (default: the newest).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON payload.")
def skills_show(package_ref: str, revision: int | None, as_json: bool):
    """Show catalog package PACKAGE — its revisions and one revision's SKILL.md.

    PACKAGE is a package id, a display name, or a UUID.
    """
    from cinna.account import run_skills_show

    run_skills_show(package_ref, revision=revision, as_json=as_json)


@skills.command(name="revisions")
@click.argument("package_ref", metavar="PACKAGE")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON listing.")
def skills_revisions(package_ref: str, as_json: bool):
    """List PACKAGE's revisions, newest first.

    Number, version, release date, size and release notes. A publisher's
    question before every publish — what is already out there — answered
    without a UUID.
    """
    from cinna.account import run_skills_revisions

    run_skills_revisions(package_ref, as_json=as_json)


@skills.command(name="files")
@click.argument("package_ref", metavar="PACKAGE")
@click.option(
    "--revision", type=int, default=None, help="Which revision (default: the newest)."
)
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON listing.")
def skills_files(package_ref: str, revision: int | None, as_json: bool):
    """List the files one revision of PACKAGE ships."""
    from cinna.account import run_skills_files

    run_skills_files(package_ref, revision=revision, as_json=as_json)


@skills.command(name="grants")
@click.argument("package_ref", metavar="PACKAGE")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON listing.")
def skills_grants(package_ref: str, as_json: bool):
    """Who is named on PACKAGE.

    A grant list exists on any package but is only consulted on one whose
    visibility is ``users``; the listing prints the visibility beside it so a
    stored grant is never mistaken for shared access.
    """
    from cinna.account import run_skills_grants

    run_skills_grants(package_ref, as_json=as_json)


@skills.command(name="grant")
@click.argument("package_ref", metavar="PACKAGE")
@click.option("--user", "email", required=True, metavar="EMAIL", help="Who to name.")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_grant(package_ref: str, email: str, as_json: bool):
    """Grant EMAIL access to PACKAGE.

    The post-publish half of ``cinna skills publish --grant``: sharing is a
    property of the package, so it can change without cutting a revision.
    """
    from cinna.account import run_skills_grant

    run_skills_grant(package_ref, email, as_json=as_json)


@skills.command(name="revoke")
@click.argument("package_ref", metavar="PACKAGE")
@click.option("--user", "email", required=True, metavar="EMAIL", help="Whose access.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_revoke(package_ref: str, email: str, yes: bool, as_json: bool):
    """Revoke EMAIL's access to PACKAGE.

    Revoking removes the grant, not the copy: an agent that already installed
    the package keeps the revision it holds. Asks first; ``--json`` requires
    ``--yes`` rather than assuming it.
    """
    from cinna.account import run_skills_revoke

    run_skills_revoke(package_ref, email, yes=yes, as_json=as_json)


@skills.command(name="visibility")
@click.argument("package_ref", metavar="PACKAGE")
@click.argument("visibility", type=click.Choice(["public", "private", "users"]))
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_visibility(package_ref: str, visibility: str, as_json: bool):
    """Set who may see PACKAGE: public, private, or users.

    ``users`` consults the package's grant list; the other two ignore it
    entirely. Switching to ``users`` with nobody named shares the package with
    nobody, and says so.
    """
    from cinna.account import run_skills_visibility

    run_skills_visibility(package_ref, visibility, as_json=as_json)


@skills.command(name="relist")
@click.argument("package_ref", metavar="PACKAGE")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_relist(package_ref: str, as_json: bool):
    """Put delisted PACKAGE back in the catalog.

    The undo for ``delist``, which has no inverse route of its own — without
    this verb the only way back would be ``cinna api``.
    """
    from cinna.account import run_skills_relist

    run_skills_relist(package_ref, as_json=as_json)


@skills.command(name="delist")
@click.argument("package_ref", metavar="PACKAGE")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON result.")
def skills_delist(package_ref: str, yes: bool, as_json: bool):
    """Take PACKAGE out of the catalog.

    Delisting hides it from discovery. Agents that already installed it keep
    what they have — a revision they hold is a copy, not a reference. Asks
    first; ``--json`` requires ``--yes`` rather than assuming it.
    """
    from cinna.account import run_skills_delist

    run_skills_delist(package_ref, yes=yes, as_json=as_json)


# ─── api (escape hatch) ────────────────────────────────────────────────────


@cli.command(name="api")
@click.argument(
    "method",
    type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE"], case_sensitive=False),
)
@click.argument("path")
@click.option("--json", "json_text", default=None, help="Inline JSON request body")
@click.option(
    "--data",
    "data_file",
    default=None,
    help="JSON request body from a file (@file.json)",
)
@click.option(
    "--query",
    "query_pairs",
    multiple=True,
    help="Query parameter as key=value (repeatable)",
)
def api_cmd(
    method: str,
    path: str,
    json_text: str | None,
    data_file: str | None,
    query_pairs: tuple[str, ...],
):
    """Call the platform API through the account escape hatch.

    PATH is relative to the API root (no /api/v1 prefix), e.g. ``agents`` or
    ``agents/<id>``. The catalogue of callable endpoints lives in the account
    workspace's ``context/api_reference/`` (see ``cinna account
    refresh-context``). Excluded categories — credentials, user management,
    admin, CLI, MFA/auth, streaming routes — are denied by the platform;
    don't waste calls on them.

    The inner response is passed through verbatim: the body is printed to
    stdout (pretty-printed for JSON) and the exit code is 0 for 2xx, 1 for an
    inner 4xx/5xx, and 2 when the escape hatch itself refuses the call
    (policy denial, rate limit, size cap — reported on stderr).

    Examples:
      cinna api GET agents
      cinna api GET agents --query limit=5
      cinna api POST agents/<id>/duplicate
      cinna api PATCH agents/<id> --json '{"description": "updated"}'
      cinna api POST tasks --data @task.json
    """
    from cinna.account import run_api

    run_api(method, path, json_text, data_file, query_pairs)


# ─── exec ──────────────────────────────────────────────────────────────────


@cli.command(name="exec", context_settings={"ignore_unknown_options": True})
@click.option(
    "--timeout",
    "-t",
    type=click.IntRange(min=1, max=86400),
    default=1800,
    show_default=True,
    help="Max wall-clock seconds the remote command may run before being killed.",
)
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Run against a synced agent from the account workspace (name, slug, or id).",
)
@click.argument("command", nargs=-1, required=True)
def exec_cmd(timeout: int, agent_ref: str | None, command: tuple[str, ...]):
    """Run a command in the remote agent environment.

    Output streams back in real time via the platform. Exit code matches the
    remote process's exit code. Ctrl+C aborts the stream.

    Arguments are passed through transparently: each token you type is
    re-quoted (``shlex.quote``) before being sent, so spaces and shell
    metacharacters inside an argument survive the remote shell intact. Use
    ordinary single-level quoting, exactly as for a local command.

    With ``--agent``, runs from an account workspace against the named synced
    agent (using that child workspace's own token). The agent must already be
    synced (``cinna agent sync <agent>``).

    Examples:
      cinna exec python scripts/main.py
      cinna exec pip install pandas
      cinna exec bash -c 'ls -la'
      cinna exec python -c 'import sys; print(sys.argv)' "a b"
      cinna exec --timeout 3600 python long_backfill.py
      cinna exec --agent crm-agent python scripts/main.py

    If your remote command takes its own ``--timeout`` flag, separate it
    from cinna's option with ``--``:

      cinna exec --timeout 3600 -- python tool.py --timeout 30
    """
    if agent_ref is not None:
        from cinna.account import find_account_root, resolve_child_workspace

        account_root = find_account_root()
        resolved = resolve_child_workspace(account_root, agent_ref)
        if resolved is None:
            raise click.ClickException(
                f"Agent '{agent_ref}' is not synced in this account workspace.\n"
                f"Run 'cinna agent sync {agent_ref}' first."
            )
        _root, config = resolved
    else:
        root = find_workspace_root()
        config = load_config(root)

    exit_code = _run_remote_exec(config, shlex.join(command), timeout=timeout)
    sys.exit(exit_code)


def _run_remote_exec(config, command_str: str, timeout: int = 1800) -> int:
    """Drive the /exec SSE stream and mirror events to the local terminal."""
    exit_code = 0
    exec_id: str | None = None
    started_at = time.monotonic()
    stdout_bytes = 0
    stderr_bytes = 0
    first_delta_at: float | None = None
    terminal_event: str = "no-terminal-event"

    logger.info(
        "exec start: agent=%s timeout=%ds cmd=%r",
        config.agent_id,
        timeout,
        command_str,
    )

    with PlatformClient(config) as client:
        try:
            for event in client.stream_exec(
                config.agent_id, command_str, timeout=timeout
            ):
                etype = event.get("type")
                if etype == "exec_id":
                    exec_id = event.get("exec_id")
                    logger.debug("exec_id assigned: %s", exec_id)
                    continue
                if etype == "tool_result_delta":
                    chunk = event.get("content", "")
                    stream = event.get("metadata", {}).get("stream", "stdout")
                    target = sys.stderr if stream == "stderr" else sys.stdout
                    target.write(chunk)
                    target.flush()
                    nbytes = len(chunk.encode("utf-8", errors="replace"))
                    if stream == "stderr":
                        stderr_bytes += nbytes
                    else:
                        stdout_bytes += nbytes
                    if first_delta_at is None:
                        first_delta_at = time.monotonic()
                        logger.debug(
                            "exec first output (stream=%s, %d bytes) after %.3fs",
                            stream,
                            nbytes,
                            first_delta_at - started_at,
                        )
                elif etype == "done":
                    exit_code = int(event.get("exit_code", 0))
                    terminal_event = "done"
                    logger.debug("exec done event: exit_code=%s", exit_code)
                elif etype == "interrupted":
                    exit_code = int(event.get("exit_code", 130))
                    terminal_event = "interrupted"
                    logger.info("exec interrupted by remote: exit_code=%s", exit_code)
                elif etype == "error":
                    msg = event.get("content", "unknown error")
                    console.error(msg)
                    exit_code = 1
                    terminal_event = "error"
                    logger.error("exec remote error: %s", msg)
                else:
                    logger.debug("exec unknown event type=%r: %.200s", etype, event)
        except KeyboardInterrupt:
            exit_code = 130
            terminal_event = "keyboard-interrupt"
            logger.info("exec interrupted locally (Ctrl-C)")

    duration = time.monotonic() - started_at
    logger.info(
        "exec stop: agent=%s exec_id=%s exit_code=%s duration=%.3fs "
        "stdout=%dB stderr=%dB terminal=%s",
        config.agent_id,
        exec_id,
        exit_code,
        duration,
        stdout_bytes,
        stderr_bytes,
        terminal_event,
    )
    return exit_code


# ─── chat ──────────────────────────────────────────────────────────────────


@cli.command(name="chat", context_settings={"ignore_unknown_options": True})
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Agent to talk to (name, slug, or id). Omit to infer from the current "
    "agent workspace.",
)
@click.option(
    "--resume",
    default=None,
    metavar="SESSION_ID",
    help="Continue an existing session instead of starting a new one.",
)
@click.option(
    "--attach",
    "attach",
    default=None,
    metavar="SESSION_ID",
    help="Send nothing: wait for SESSION_ID's current turn to finish and print "
    "it — the recovery when a chat command died while the agent kept working.",
)
@click.option(
    "--show",
    "show",
    default=None,
    metavar="SESSION_ID",
    help="Send nothing, wait for nothing: print SESSION_ID's transcript.",
)
@click.option(
    "--file",
    "files",
    multiple=True,
    type=click.Path(),
    help="Attach a local file to the message (repeatable).",
)
@click.option(
    "--mode",
    type=click.Choice(["conversation", "building"]),
    default="conversation",
    show_default=True,
    help="Session mode for a NEW session.",
)
@click.option("--title", default=None, help="Title for a NEW session.")
@click.option(
    "--download-dir",
    default=None,
    help="Where to save attachments the agent produces "
    "(default: ./cinna-chat-files/<session_id>/).",
)
@click.option(
    "--no-download",
    is_flag=True,
    help="Don't download agent attachments — just report their file ids.",
)
@click.option(
    "--interval",
    type=float,
    default=2.0,
    show_default=True,
    help="Seconds between polls while the agent is responding.",
)
@click.option(
    "--timeout",
    type=int,
    default=600,
    show_default=True,
    help="Max seconds to wait for the agent's turn to finish.",
)
@click.option(
    "--events/--no-events",
    "include_events",
    default=True,
    show_default=True,
    help="Include the agent's reasoning/tool trace (thinking blocks, tool calls "
    "with payloads, tool results) on each message — not just the final text.",
)
@click.option(
    "--pretty",
    is_flag=True,
    help="Human-readable output instead of the default NDJSON event stream.",
)
@click.argument("message", nargs=-1, type=click.UNPROCESSED)
def chat_cmd(
    agent_ref: str | None,
    resume: str | None,
    attach: str | None,
    show: str | None,
    files: tuple[str, ...],
    mode: str,
    title: str | None,
    download_dir: str | None,
    no_download: bool,
    interval: float,
    timeout: int,
    include_events: bool,
    pretty: bool,
    message: tuple[str, ...],
):
    """Chat with an agent through a real platform session.

    Runs the message through the production conversation pipeline (permission
    checks, agent-env calls, the model/SDK the platform selects) rather than any
    local mock — so a coding agent can test the agent it is building. The reply
    is observed by polling the backend, so it is robust to streaming/transport
    quirks.

    By default each new message/event is printed as one JSON object per line
    (NDJSON) — easy for another agent to parse. Each message carries the agent's
    reasoning/tool trace under "events" (thinking blocks, tool calls with their
    payloads, tool results); pass --no-events for just the final text. Use
    --pretty for a human view. Files the agent attaches to its replies are
    downloaded locally for inspection; attach your own with --file.

    Run it from your account workspace (or any synced agent folder under it). If
    no message is given and you're in a TTY, you'll be prompted for one;
    otherwise it is read from stdin.

    \b
    Examples:
      cinna chat --agent crm-agent "Summarize today's leads"
      cinna chat --agent crm-agent --file report.csv "Validate this export"
      cinna chat --resume 3f2c… "And now break it down by region"
      echo "ping" | cinna chat --agent crm-agent
      cinna chat --attach 3f2c…    # wait for a turn a dead command left running
      cinna chat --show 3f2c…      # print a session's transcript

    A poll request that fails (a proxy timeout, a 5xx) is retried within
    --timeout. If contact is lost for good, the error names the session and
    the --attach command: the agent's turn keeps running on the platform.
    The closing "done" event carries "outcome": completed, timeout or
    not_started (in_progress or idle for --show).
    """
    if attach or show:
        if attach and show:
            raise click.UsageError("--attach and --show are alternatives; pass one.")
        flag = "--attach" if attach else "--show"
        extras = [
            name
            for name, value in (
                ("--resume", resume),
                ("--file", files),
                ("--agent", agent_ref),
                ("message", message),
            )
            if value
        ]
        if extras:
            raise click.UsageError(
                f"{flag} names an existing session and sends nothing, so it "
                f"takes no {' or '.join(extras)}."
            )
        from cinna.chat import run_chat_attach, run_chat_show

        if attach:
            run_chat_attach(
                attach,
                download_dir=download_dir,
                no_download=no_download,
                interval=interval,
                timeout=timeout,
                pretty=pretty,
                include_events=include_events,
            )
        else:
            run_chat_show(
                show,
                download_dir=download_dir,
                no_download=no_download,
                pretty=pretty,
                include_events=include_events,
            )
        return

    from cinna.chat import run_chat

    run_chat(
        agent_ref=agent_ref,
        resume=resume,
        message_tokens=message,
        files=files,
        mode=mode,
        title=title,
        download_dir=download_dir,
        no_download=no_download,
        interval=interval,
        timeout=timeout,
        pretty=pretty,
        include_events=include_events,
    )


# ─── status ────────────────────────────────────────────────────────────────


@cli.command()
def status():
    """Show agent info and current sync state.

    Works in a per-agent workspace (sync state + token) and in an *account*
    workspace (``.cinna/account.json`` from ``cinna account setup`` / ``cinna
    login``), where it shows the account session and token validity instead of
    failing — the same view as ``cinna account status``.
    """
    from cinna.errors import ConfigNotFoundError

    try:
        root = find_workspace_root()
    except ConfigNotFoundError:
        # Not a per-agent workspace. The folder may still be an account
        # workspace (these only carry account.json, not config.json); show the
        # account session there rather than the generic "not a workspace" error.
        from cinna.account import find_account_root, run_account_status
        from cinna.errors import AccountConfigNotFoundError

        try:
            find_account_root()
        except AccountConfigNotFoundError:
            raise ConfigNotFoundError() from None
        run_account_status()
        return

    config = load_config(root)

    from rich.table import Table

    st = sync_session.status(config)

    with console.spinner("Checking token..."):
        token_status = _probe_token_statuses(
            [
                {
                    "agent_id": config.agent_id,
                    "platform_url": config.platform_url,
                    "cli_token": config.cli_token,
                }
            ]
        ).get(config.agent_id, "unknown")

    table = Table(title=f"Agent: {config.agent_name}")
    table.add_column("Property", style="dim")
    table.add_column("Value")
    table.add_row("Platform", config.platform_url)
    table.add_row("Agent ID", config.agent_id)
    table.add_row("Template", config.template)
    table.add_row("Mutagen", config.mutagen_version or "—")
    table.add_row("Sync state", _colored_state(st.state))
    table.add_row("Token", _format_token_label(token_status))
    table.add_row("Pending → remote", str(st.pending_to_remote))
    table.add_row("Pending → local", str(st.pending_to_local))
    table.add_row("Conflicts", str(st.conflict_count))
    if st.last_error:
        table.add_row("Last error", f"[red]{st.last_error}[/red]")

    console.console.print(table)


def _colored_state(state: str) -> str:
    if state == "connected":
        return "[green]connected[/green]"
    if state == "paused":
        return "[yellow]paused[/yellow]"
    if state in {"error", "missing"}:
        return f"[red]{state}[/red]"
    return state


# ─── sync group ────────────────────────────────────────────────────────────


@cli.command("list")
def list_cmd():
    """List every agent registered on this machine.

    Reads ``~/.cinna/agents.json`` — the same registry the SSH shim uses to
    resolve per-agent credentials. For each agent the table shows agent ID,
    the web UI link, workspace path, current sync state, and whether the
    stored CLI token is still accepted by the backend. Workspace directories
    that no longer exist are flagged as missing; clean them up — together with
    halted/orphaned sessions and expired tokens — with ``cinna doctor``.
    """
    from rich.table import Table

    entries = list_agent_registry()
    if not entries:
        console.status(
            "No agents registered yet. Run the setup curl command to register one."
        )
        return

    # Cheap one-shot lookup: index Mutagen sessions by session name so we can
    # report per-agent sync state without a daemon round-trip per row.
    # Fails silently if the daemon isn't running — sync just reads "–".
    sessions_by_name: dict[str, dict] = {}
    try:
        # sync_session._list_sessions needs a CinnaConfig for env vars. Build
        # a throwaway one off the first entry; MUTAGEN_SSH_PATH is the only
        # env var that matters for `sync list` and it's the same for every
        # agent on this machine.
        from cinna.sync_session import _list_sessions, CinnaConfig as _Cfg

        probe_entry = entries[0]
        probe = _Cfg(
            platform_url=probe_entry.get("platform_url", ""),
            cli_token=probe_entry.get("cli_token", ""),
            agent_id=probe_entry["agent_id"],
            agent_name="",
            environment_id="",
            template="",
        )
        for s in _list_sessions(probe):
            name = s.get("name")
            if name:
                sessions_by_name[name] = s
    except Exception:
        pass

    with console.spinner("Checking tokens..."):
        token_statuses = _probe_token_statuses(entries)

    table = Table(
        title=f"Registered agents ({len(entries)})",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Agent")
    table.add_column("Location")
    table.add_column("Sync")

    for i, entry in enumerate(entries, 1):
        agent_id = entry["agent_id"]
        platform_url = entry.get("platform_url", "")
        frontend_url = entry.get("frontend_url") or platform_url
        workspace_path = Path(entry.get("workspace_path", ""))

        # Default display = short agent_id; enrich with the agent's display
        # name if the workspace's .cinna/config.json is still intact.
        display_name = agent_id[:8]
        if workspace_path and workspace_path.exists():
            ws_display = str(workspace_path)
            try:
                cfg = load_config(workspace_path)
                display_name = cfg.agent_name
            except Exception:
                pass
        else:
            ws_display = f"[red]missing:[/red] {workspace_path or '?'}"

        agent_link = (
            f"{frontend_url.rstrip('/')}/agent/{agent_id}" if frontend_url else "?"
        )
        sync_cell = _format_sync_cell(
            agent_id, sessions_by_name, token_statuses.get(agent_id, "unknown")
        )

        agent_cell = f"[bold]{display_name}[/bold]\n[dim]{agent_id}[/dim]"
        location_cell = f"{ws_display}\n[dim]{agent_link}[/dim]"

        table.add_row(
            str(i),
            agent_cell,
            location_cell,
            sync_cell,
        )

    console.console.print(table)


def _probe_token_statuses(entries: list[dict]) -> dict[str, str]:
    """Check each agent's backend in parallel and classify the CLI token.

    Returns a mapping ``agent_id -> status`` where status is one of:
      - ``valid``       — backend answered 2xx
      - ``expired``     — backend answered 401
      - ``unreachable`` — connection/timeout/other error
    """
    from concurrent.futures import ThreadPoolExecutor

    def probe(entry: dict) -> tuple[str, str]:
        agent_id = entry["agent_id"]
        platform_url = (entry.get("platform_url") or "").rstrip("/")
        cli_token = entry.get("cli_token") or ""
        if not platform_url or not cli_token:
            return agent_id, "unreachable"
        try:
            import httpx

            response = httpx.get(
                f"{platform_url}/api/v1/cli/agents/{agent_id}/sync-runtime",
                headers={"Authorization": f"Bearer {cli_token}"},
                timeout=httpx.Timeout(5.0, connect=3.0),
                follow_redirects=True,
            )
        except Exception:
            return agent_id, "unreachable"
        if response.status_code == 401:
            return agent_id, "expired"
        if 200 <= response.status_code < 300:
            return agent_id, "valid"
        return agent_id, "unreachable"

    results: dict[str, str] = {}
    max_workers = min(8, max(1, len(entries)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for agent_id, status in pool.map(probe, entries):
            results[agent_id] = status
    return results


def _format_sync_cell(
    agent_id: str,
    sessions_by_name: dict[str, dict],
    token_status: str = "unknown",
) -> str:
    """Render the Sync column for one row.

    Top line is the Mutagen session state (running / paused / error / idle);
    bottom line reports whether the stored CLI token is still accepted by the
    backend.
    """
    from cinna.sync_session import session_name

    session = sessions_by_name.get(session_name(agent_id))
    if session is None:
        sync_label = "[dim]–[/dim]"
    elif session.get("paused"):
        sync_label = "[yellow]paused[/yellow]"
    elif session.get("lastError"):
        sync_label = "[red]error[/red]"
    else:
        alpha_conn = bool((session.get("alpha") or {}).get("connected"))
        beta_conn = bool((session.get("beta") or {}).get("connected"))
        if alpha_conn and beta_conn:
            sync_label = "[green]active[/green]"
        else:
            sync_label = "[yellow]connecting[/yellow]"

    token_label = _format_token_label(token_status)
    return f"{sync_label}\n{token_label}"


def _format_token_label(status: str) -> str:
    if status == "valid":
        return "[green]valid token[/green]"
    if status == "expired":
        return "[red]expired token[/red]"
    if status == "unreachable":
        return "[yellow]no connection[/yellow]"
    return "[dim]–[/dim]"


# ─── doctor ──────────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report problems but make no changes.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Apply every fix without prompting per category.",
)
def doctor(dry_run: bool, yes: bool):
    """Diagnose and repair stale sync state on this machine.

    Reconciles the per-user registry (``~/.cinna/agents.json``) against the
    Mutagen daemon and heals the leftovers that pile up as agents come and go:
    registry entries whose workspace was deleted, sessions halted on a deleted
    local root, sessions stuck retrying a remote env that is gone, and orphaned
    sessions with no registry entry. Expired CLI tokens are re-minted
    automatically for account-managed workspaces (no pasted token); standalone
    workspaces are reported for a manual ``cinna set-token``.

    Mutagen has no "stop retrying after N failures" option — a session retries a
    dead remote forever — so this is the cleanup path for those sessions.
    The live Mutagen session inventory — each tagged with its agent and folder —
    is shown up front, then repairs run as three ordered, separately-confirmed
    steps (each defaulting to Yes): delete stalled sessions, terminate active
    sessions (recreated on the next ``cinna dev``), and refresh expired tokens.
    Standalone expired tokens are reported as "manual action needed" and never
    touched. ``--yes`` accepts every prompt; ``--dry-run`` only reports.
    """
    from cinna.doctor import run_doctor

    run_doctor(dry_run=dry_run, yes=yes)


@cli.command()
def dev():
    """Start a foreground dev session: live workspace sync + TUI.

    Creates the Mutagen sync session for this agent and attaches the terminal
    to a two-tab TUI (status + raw Mutagen details). Ctrl-C terminates the
    session — sync does not outlive the TUI. To observe sync from another
    terminal without affecting it, use ``cinna sync status``.
    """
    _run_dev_session(favor_remote=False)


@cli.command()
def redev():
    """Start a dev session, resolving startup conflicts in favor of remote.

    Identical to ``cinna dev``, except conflicts surfaced by the initial
    reconciliation — files that changed on both sides since the last session
    — are resolved automatically with the remote version winning. Use it to
    resume work on an agent that was modified from the platform side while
    your local copy sat idle, without re-running setup.

    The displaced local versions are backed up under
    ``.cinna/sync/redev-backup/<timestamp>/`` before being overwritten.
    Only startup conflicts are auto-resolved; conflicts that arise later in
    the session are surfaced normally in the Conflicts tab.
    """
    _run_dev_session(favor_remote=True)


def _run_dev_session(favor_remote: bool) -> None:
    """Shared body of ``cinna dev`` / ``cinna redev``."""
    root = find_workspace_root()
    config = load_config(root)

    with PlatformClient(config) as client:
        ensure_mutagen_ready(client, config, root, interactive=console.interactive())

    st = sync_session.start(config, root)

    if favor_remote:
        console.status(f"Sync session created ({st.state}).")
        with console.spinner("Reconciling with remote (remote wins conflicts)…"):
            res = sync_session.resolve_startup_conflicts_favor_remote(config, root)
        if res.resolved:
            console.status(
                f"Resolved {len(res.resolved)} conflict(s) in favor of remote."
            )
            if res.backup_dir is not None:
                console.status(f"Local versions backed up to {res.backup_dir}")
        else:
            console.status("No conflicts — local workspace is consistent with remote.")
        if res.remaining:
            console.warn(
                f"{len(res.remaining)} conflict(s) could not be auto-resolved — "
                "resolve them in the Conflicts tab."
            )
        console.status("Attaching live view. Press Ctrl-C to stop.")
    else:
        console.status(
            f"Sync session created ({st.state}) — attaching live view. Press Ctrl-C to stop."
        )

    sync_session.run_foreground(config, root)
    console.status("Sync session terminated.")


def _resolve_sync_target(agent_ref: str | None):
    """Resolve a sync command's (workspace_root, config).

    Without ``--agent`` it uses the current workspace. With ``--agent`` it
    resolves a child workspace synced under the account workspace — so every
    ``cinna sync`` subcommand works from the account root, consistent with
    ``cinna exec --agent``.
    """
    if agent_ref is not None:
        from cinna.account import find_account_root, resolve_child_workspace

        account_root = find_account_root()
        resolved = resolve_child_workspace(account_root, agent_ref)
        if resolved is None:
            raise click.ClickException(
                f"Agent '{agent_ref}' is not synced in this account workspace.\n"
                f"Run 'cinna agent sync {agent_ref}' first."
            )
        return resolved  # (root, config)
    root = find_workspace_root()
    return root, load_config(root)


def _make_remote_deleter(config):
    """Return a ``delete(relpath) -> bool`` that removes a remote workspace file.

    Used by local-wins conflict resolution: the losing copy lives on the remote
    agent, so we shell it out through the exec stream (``rm -f -- <path>``) and
    report success from the terminal ``done`` event's exit code.
    """

    def _delete(relpath: str) -> bool:
        remote_path = f"/app/workspace/{relpath}"
        cmd = f"rm -f -- {shlex.quote(remote_path)}"
        exit_code = 1
        try:
            with PlatformClient(config) as client:
                for event in client.stream_exec(config.agent_id, cmd, timeout=60):
                    etype = event.get("type")
                    if etype == "done":
                        exit_code = int(event.get("exit_code", 0))
                    elif etype == "error":
                        logger.error(
                            "remote rm error for %s: %s", relpath, event.get("content")
                        )
                        return False
        except Exception as exc:  # network / stream failure
            logger.error("remote rm failed for %s: %s", relpath, exc)
            return False
        return exit_code == 0

    return _delete


@cli.group()
def sync():
    """Inspect and drive the continuous workspace sync session.

    ``status`` / ``conflicts`` are read-only views (safe to run alongside a live
    ``cinna dev``). ``push`` / ``pull`` are one-shot, blocking flushes for
    scripted (headless) builders, and ``resolve`` clears parked conflicts. All
    subcommands accept ``--agent <ref>`` to target a synced child workspace from
    the account root.
    """


@sync.command("status")
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Target a synced agent from the account root",
)
def sync_status(agent_ref: str | None):
    """Print the sync session state."""
    root, config = _resolve_sync_target(agent_ref)

    st = sync_session.status(config)
    from rich.table import Table

    table = Table(title=f"Sync — {config.agent_name}")
    table.add_column("Property", style="dim")
    table.add_column("Value")
    table.add_row("Session", st.session_name)
    table.add_row("State", _colored_state(st.state))
    table.add_row("Pending → remote", str(st.pending_to_remote))
    table.add_row("Pending → local", str(st.pending_to_local))
    table.add_row("Conflicts", str(st.conflict_count))
    if st.last_error:
        table.add_row("Last error", f"[red]{st.last_error}[/red]")
    console.console.print(table)
    if st.conflict_count:
        console.console.print(
            "\n[yellow]⚠ Your edits are NOT fully live — "
            f"{st.conflict_count} file(s) conflicted.[/yellow] "
            "Run 'cinna sync conflicts' to list them, then "
            "'cinna sync resolve --prefer local' (or remote)."
        )


@sync.command("conflicts")
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Target a synced agent from the account root",
)
@click.option(
    "--diff",
    "show_diff",
    is_flag=True,
    help="Compare each conflicted file's local and remote copies: size, sha256, "
    "and a unified diff (remote → local) for text files.",
)
def sync_conflicts(agent_ref: str | None, show_diff: bool):
    """List sync conflicts the Mutagen daemon has parked.

    Sources from the daemon's conflict list (authoritative), so this agrees
    with the count shown by ``cinna sync status`` — two-way-safe does not write
    ``.conflict.*`` files on disk, so a disk walk would always look empty.

    With ``--diff`` each path is compared across the two sides, so choosing a
    winner no longer takes a manual ``cinna exec cat`` per file.
    """
    root, config = _resolve_sync_target(agent_ref)

    paths = sync_session.daemon_conflict_paths(config)
    if not paths:
        console.status("✓ No conflicts.")
        return

    from rich.table import Table

    table = Table(title=f"Conflicts ({len(paths)})")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Path (relative to workspace/)")
    for i, p in enumerate(paths, 1):
        table.add_row(str(i), p)
    console.console.print(table)
    if show_diff:
        _print_conflict_diffs(root, config, paths)
    console.console.print(
        "\nResolve with 'cinna sync resolve --prefer local' (your local edits "
        "win) or '--prefer remote' (the container's version wins)."
    )


# Text above this size is compared by hash only — a diff of it would not be read.
_CONFLICT_TEXT_LIMIT = 200_000
_CONFLICT_DIFF_LINES = 80

# Runs inside the agent's container: one JSON line of facts per requested path.
# Content travels only for small UTF-8 files, and only to render the diff.
_REMOTE_FACTS_SCRIPT = f"""\
import hashlib, json, sys
out = {{}}
for rel in sys.argv[1:]:
    try:
        with open("/app/workspace/" + rel, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        out[rel] = {{"exists": False}}
        continue
    except IsADirectoryError:
        out[rel] = {{"exists": True, "directory": True}}
        continue
    facts = {{"exists": True, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
    if len(data) <= {_CONFLICT_TEXT_LIMIT}:
        try:
            facts["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    out[rel] = facts
print(json.dumps(out))
"""


def _local_file_facts(path: Path) -> dict:
    """The same facts the remote script reports, for the local copy."""
    import hashlib

    if path.is_dir():
        return {"exists": True, "directory": True}
    if not path.is_file():
        return {"exists": False}
    data = path.read_bytes()
    facts: dict = {
        "exists": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) <= _CONFLICT_TEXT_LIMIT:
        try:
            facts["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return facts


def _remote_file_facts(config, relpaths: list[str]) -> dict | None:
    """Facts for each remote path in one exec, or ``None`` when unreadable."""
    import json

    cmd = shlex.join(["python3", "-c", _REMOTE_FACTS_SCRIPT, *relpaths])
    stdout: list[str] = []
    exit_code = 1
    try:
        with PlatformClient(config) as client:
            for event in client.stream_exec(config.agent_id, cmd, timeout=120):
                etype = event.get("type")
                if etype == "tool_result_delta":
                    if (event.get("metadata") or {}).get("stream", "stdout") == "stdout":
                        stdout.append(event.get("content") or "")
                elif etype == "done":
                    exit_code = int(event.get("exit_code", 0))
                elif etype == "error":
                    logger.error("remote facts error: %s", event.get("content"))
                    return None
    except Exception as exc:  # network / stream failure
        logger.error("remote facts failed: %s", exc)
        return None
    if exit_code != 0:
        return None
    lines = "".join(stdout).strip().splitlines()
    try:
        parsed = json.loads(lines[-1])
    except (ValueError, IndexError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _facts_line(facts: dict | None) -> str:
    if facts is None:
        return "[dim]unknown[/dim]"
    if not facts.get("exists"):
        return "[yellow]missing[/yellow]"
    if facts.get("directory"):
        return "directory"
    return f"{facts['size']:,} bytes  [dim]sha256 {facts['sha256'][:12]}[/dim]"


def _conflict_verdict(local: dict, remote: dict | None) -> str | None:
    """The one conclusion the facts support, when there is one."""
    if remote is None:
        return None
    if local.get("exists") and not remote.get("exists"):
        return "Only the local copy exists."
    if remote.get("exists") and not local.get("exists"):
        return "Only the remote copy exists."
    if local.get("sha256") and local.get("sha256") == remote.get("sha256"):
        return "Identical content — resolving either way changes nothing."
    return None


def _print_conflict_diffs(root: Path, config, relpaths: list[str]) -> None:
    """Local vs remote facts per conflicted path, plus a diff for text."""
    import difflib

    from rich.markup import escape

    with console.spinner("Reading the remote copies…"):
        remote = _remote_file_facts(config, relpaths)
    if remote is None:
        console.warn(
            "Could not read the remote copies (is the environment running?) — "
            "showing the local side only."
        )
    workspace = sync_session.workspace_dir(root)
    for i, rel in enumerate(relpaths, 1):
        local = _local_file_facts(workspace / rel)
        far = None if remote is None else remote.get(rel)
        console.console.print()
        console.console.print(f"[bold]{i}. {escape(rel)}[/bold]")
        console.console.print(f"   local : {_facts_line(local)}")
        console.console.print(f"   remote: {_facts_line(far)}")
        verdict = _conflict_verdict(local, far)
        if verdict:
            console.console.print(f"   [dim]{verdict}[/dim]")
        if (
            far is not None
            and local.get("text") is not None
            and far.get("text") is not None
            and local.get("sha256") != far.get("sha256")
        ):
            diff = list(
                difflib.unified_diff(
                    far["text"].splitlines(),
                    local["text"].splitlines(),
                    fromfile=f"remote/{rel}",
                    tofile=f"local/{rel}",
                    lineterm="",
                )
            )
            for line in diff[:_CONFLICT_DIFF_LINES]:
                click.echo(f"   {line}")
            if len(diff) > _CONFLICT_DIFF_LINES:
                console.console.print(
                    f"   [dim]… {len(diff) - _CONFLICT_DIFF_LINES} more diff line(s)[/dim]"
                )


@sync.command("push")
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Target a synced agent from the account root",
)
@click.option(
    "--force",
    is_flag=True,
    help="Local wins: clear conflicts in favor of local before flushing",
)
def sync_push(agent_ref: str | None, force: bool):
    """Flush local → remote once and block until settled (headless-friendly).

    Ensures a sync session exists (reusing a live ``cinna dev`` session, or
    creating a detached one that persists in the daemon), then forces a sync
    cycle and waits for it to settle. With ``--force``, any parked conflicts are
    resolved in favor of your local copy first ("my local is the truth").
    """
    root, config = _resolve_sync_target(agent_ref)
    with console.spinner("Ensuring sync session…"):
        sync_session.ensure_session(config, root)

    if force:
        with console.spinner("Resolving conflicts in favor of local…"):
            res = sync_session.resolve_conflicts(
                config,
                root,
                prefer="local",
                remote_delete=_make_remote_deleter(config),
            )
        if res.resolved:
            console.status(
                f"Resolved {len(res.resolved)} conflict(s) in favor of local."
            )
        if res.remaining:
            console.warn(
                f"{len(res.remaining)} conflict(s) could not be resolved: {', '.join(res.remaining)}"
            )

    # `mutagen sync flush` is bidirectional; the push/pull distinction is the
    # --force resolution direction, not the flush itself.
    with console.spinner("Flushing sync…"):
        st = sync_session.flush(config)
    _report_flush(st, winner="local")


@sync.command("pull")
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Target a synced agent from the account root",
)
@click.option(
    "--force",
    is_flag=True,
    help="Remote wins: clear conflicts in favor of remote before flushing",
)
def sync_pull(agent_ref: str | None, force: bool):
    """Flush remote → local once and block until settled.

    The mirror of ``push``: ensures a session, optionally resolves conflicts in
    favor of the remote (``--force``), then flushes and waits to settle. Useful
    after the backend regenerates managed files (prompts, credentials).
    """
    root, config = _resolve_sync_target(agent_ref)
    with console.spinner("Ensuring sync session…"):
        sync_session.ensure_session(config, root)

    if force:
        with console.spinner("Resolving conflicts in favor of remote…"):
            res = sync_session.resolve_conflicts(config, root, prefer="remote")
        if res.resolved:
            console.status(
                f"Resolved {len(res.resolved)} conflict(s) in favor of remote."
            )
            if res.backup_dir is not None:
                console.status(f"Local versions backed up to {res.backup_dir}")
        if res.remaining:
            console.warn(
                f"{len(res.remaining)} conflict(s) could not be resolved: {', '.join(res.remaining)}"
            )

    # `mutagen sync flush` is bidirectional; the push/pull distinction is the
    # --force resolution direction, not the flush itself.
    with console.spinner("Flushing sync…"):
        st = sync_session.flush(config)
    _report_flush(st, winner="remote")


def _report_flush(st, winner: str) -> None:
    """Say how a one-shot flush ended — never "settled" over parked conflicts.

    A flush that finished with conflicts did not settle: the conflicted files
    are exactly the edits that are not live. Leading with a green check there
    is how a push that changed nothing read as a success.
    """
    if not st.conflict_count:
        console.status(f"Sync settled ({st.state}).")
        return
    note = " — your edits are NOT fully live" if winner == "local" else ""
    console.warn(
        f"Sync flushed ({st.state}), but {st.conflict_count} conflict(s) "
        f"remain{note}. See what differs with 'cinna sync conflicts --diff', then "
        f"re-run with --force ({winner} wins) or 'cinna sync resolve'."
    )


@sync.command("resolve")
@click.option(
    "--prefer",
    type=click.Choice(["local", "remote"]),
    required=True,
    help="Which side wins",
)
@click.option(
    "--agent",
    "agent_ref",
    default=None,
    help="Target a synced agent from the account root",
)
def sync_resolve(prefer: str, agent_ref: str | None):
    """Clear parked sync conflicts in favor of local or remote.

    ``--prefer local`` keeps your local edits (the remote losing copies are
    deleted and your version propagates out); ``--prefer remote`` keeps the
    container's version (your local copies are backed up under .cinna/sync/ and
    the remote propagates back). The one-command replacement for the manual
    kill/delete/restart dance.
    """
    root, config = _resolve_sync_target(agent_ref)

    st = sync_session.status(config)
    if not st.exists:
        raise click.ClickException(
            "No sync session is running. Start one with 'cinna dev' or "
            "'cinna sync push' first."
        )

    remote_delete = _make_remote_deleter(config) if prefer == "local" else None
    with console.spinner(f"Resolving conflicts in favor of {prefer}…"):
        res = sync_session.resolve_conflicts(
            config, root, prefer=prefer, remote_delete=remote_delete
        )
    if res.resolved:
        console.status(
            f"Resolved {len(res.resolved)} conflict(s) in favor of {prefer}."
        )
        if res.backup_dir is not None:
            console.status(f"Local versions backed up to {res.backup_dir}")
    else:
        console.status("No conflicts to resolve.")
    if res.remaining:
        console.warn(
            f"{len(res.remaining)} conflict(s) could not be resolved: "
            f"{', '.join(res.remaining)}"
        )


# ─── git versioning ────────────────────────────────────────────────────────


def _git_agent_opt(f):
    """Shared ``--agent`` option for the git subcommands (mirrors sync)."""
    return click.option(
        "--agent",
        "agent_ref",
        default=None,
        help="Target a synced agent from the account root",
    )(f)


@cli.group()
def git():
    """Version the agent's workspace with git against its external remote.

    Thin, fail-loud wrappers over real git that run with YOUR own git/SSH
    credentials (the platform's deploy key is never used locally). The CLI's job
    is discovering the agent's git coordinates, getting the working-tree layout
    and Mutagen wiring right, and surfacing fast-forward-only rejections clearly
    — not replacing git. See 'cinna git link' to get started.
    """


@git.command(name="link")
@_git_agent_opt
def git_link(agent_ref: str | None):
    """Turn this agent's folder into a git working tree against its remote.

    Fetches the agent's git coordinates, then (re-)initializes the clone,
    sparse-checks-out the agent's subdir, fetches the remote ref with your
    credentials, and points the index at it WITHOUT clobbering the live files —
    so the backend's in-flight changes show up as ordinary uncommitted edits
    ready to review and commit.
    """
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)
    with PlatformClient(config) as client:
        coords = git_versioning.fetch_coordinates(client)
        if not coords.vcs_enabled:
            raise click.ClickException(
                "This agent is not git-versioned. Enable Git Versioning in the "
                "agent's settings on the platform, then re-run 'cinna git link'."
            )
        with console.spinner("Linking git working tree…"):
            result = git_versioning.link(config, client, root, coords)

    console.status(
        f"Linked {config.agent_name} to {coords.repo_url} (branch {result.ref})."
    )
    console.console.print(f"  working tree: {result.clone}")
    if result.relayout_moved:
        console.warn(
            "The agent folder was moved to match the backend subdir. If a dev "
            "session was running, restart it with 'cinna dev' from the new path."
        )
    console.console.print(
        "  cinna git status / commit -m / push / pull   # standard git, fail-loud"
    )


@git.command(name="status")
@_git_agent_opt
def git_status(agent_ref: str | None):
    """Show the agent's git coordinates and working-tree status."""
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)

    if config.git is None or not config.git.vcs_enabled:
        # Surface live backend state so the user knows whether to link.
        try:
            with PlatformClient(config) as client:
                coords = git_versioning.fetch_coordinates(client)
        except Exception as exc:  # noqa: BLE001
            logger.debug("coordinates fetch failed: %s", exc)
            coords = None
        if coords is not None and coords.vcs_enabled:
            console.status(
                f"This agent is git-versioned ({coords.repo_url}, branch "
                f"{coords.effective_ref}) but not linked locally."
            )
            console.console.print("  Run 'cinna git link' to set up the working tree.")
        else:
            console.status("This agent is not git-versioned (Mutagen-only).")
        return

    g = config.git
    console.status(f"Linked: {g.repo_url} (branch {g.ref})")
    console.console.print(f"  working tree: {git_versioning.clone_root(config, root)}")
    console.console.print(f"  subdir:       {g.subdir or '(repo root)'}")
    console.console.print(f"  direction:    {g.sync_direction or 'bidirectional'}")
    text = git_versioning.status(config, root)
    console.console.print()
    console.console.print(text or "  (working tree clean)")


@git.command(name="commit")
@click.option("-m", "--message", required=True, help="Commit message")
@click.option("--push", is_flag=True, help="Push to the remote after committing")
@_git_agent_opt
def git_commit(message: str, push: bool, agent_ref: str | None):
    """Stage the agent's subdir and commit (honors the committed .gitignore)."""
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)
    changed = git_versioning.commit(config, root, message)
    if not changed:
        console.status("Nothing to commit — the working tree is clean.")
        return
    console.status("Committed.")
    if push:
        with console.spinner("Pushing to remote…"):
            git_versioning.push(config, root)
        _print_push_followup()


@git.command(name="push")
@_git_agent_opt
def git_push(agent_ref: str | None):
    """Push the agent's branch to the remote (fast-forward only)."""
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)
    with console.spinner("Pushing to remote…"):
        git_versioning.push(config, root)
    _print_push_followup()


@git.command(name="pull")
@_git_agent_opt
def git_pull(agent_ref: str | None):
    """Rebase-pull the remote into the local working tree.

    Mutagen then mirrors the updated workspace/ into the running container, so
    the live env picks up the change. (The backend's own DB-side state — prompts,
    schedules — only updates when the backend pulls.)
    """
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)
    with console.spinner("Pulling from remote…"):
        git_versioning.pull(config, root)
    console.status("Pulled. Mutagen will mirror the update into the running env.")


@git.command(name="log")
@_git_agent_opt
def git_log(agent_ref: str | None):
    """Show recent commits touching this agent's subdir."""
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)
    text = git_versioning.log_oneline(config, root)
    console.console.print(text or "No commits yet.")


@git.command(name="checkout")
@click.argument("ref")
@click.option(
    "--reload/--no-reload",
    default=True,
    help="Flush the restored files to the running env via Mutagen (default on).",
)
@click.option(
    "--manifest",
    is_flag=True,
    help="Also restore cinna.agent.json (note: not Mutagen-synced; backend reload still needed).",
)
@_git_agent_opt
def git_checkout(ref: str, reload: bool, manifest: bool, agent_ref: str | None):
    """Restore a past version's workspace files into the tree (no commit).

    Lays an earlier commit's ``workspace/**`` back into the working tree as
    uncommitted changes and (with ``--reload``) flushes them to the running
    container via Mutagen — so the live agent runs that version's prompts and
    scripts for debugging or rollback, without committing anything. Undo with
    'cinna git checkout <current-ref>' or 'cinna sync pull --force'.
    """
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)
    restored = git_versioning.checkout_version(
        config, root, ref, include_manifest=manifest
    )
    if not restored:
        console.status(f"Nothing restored from {ref}.")
        return
    console.status(f"Restored {', '.join(restored)} from {ref} (uncommitted).")

    if reload:
        with console.spinner("Flushing restored files to the running env…"):
            sync_session.ensure_session(config, root)
            st = sync_session.flush(config)
        console.status(f"Live env updated ({st.state}).")
        if st.conflict_count:
            console.warn(
                f"{st.conflict_count} conflict(s) — resolve with 'cinna sync resolve'."
            )
    if manifest:
        console.warn(
            "cinna.agent.json was restored locally but Mutagen does not sync it. "
            "To reload the manifest into the agent, use the platform UI 'Pull' on "
            "the agent's Git Versioning card (or the configured GitOps webhook)."
        )


@git.command(name="unlink")
@_git_agent_opt
def git_unlink(agent_ref: str | None):
    """Stop offering git helpers for this agent (keeps .git and history)."""
    from cinna import git_versioning

    root, config = _resolve_sync_target(agent_ref)
    git_versioning.unlink(config, root)
    console.status(
        "Git helpers disabled for this agent. Your .git/ and history are kept; "
        "re-run 'cinna git link' to re-enable."
    )


def _print_push_followup() -> None:
    console.status("Pushed.")
    console.console.print(
        "  The running agent isn't updated yet — click 'Pull' on the agent's GIT "
        "Versioning card in the web UI, or rely on the configured GitOps webhook, "
        "to apply it."
    )


# ─── disconnect ────────────────────────────────────────────────────────────


@cli.command()
def disconnect():
    """Stop sync and remove local config (workspace files preserved)."""
    root = find_workspace_root()
    config = load_config(root)

    console.warn(
        "This will stop sync, remove .cinna/ config, and delete generated files."
    )
    console.console.print("Workspace files will be preserved.")
    if not console.confirm("Continue?"):
        raise click.Abort()

    try:
        sync_session.stop(config)
    except Exception as exc:
        console.warn(f"Could not stop sync session cleanly: {exc}")

    remove_agent_registry(config.agent_id)

    from cinna.bootstrap import remove_workspace_artifacts

    remove_workspace_artifacts(root)

    console.status("Disconnected. Workspace files preserved.")


@cli.command(name="disconnect-all")
def disconnect_all():
    """Remove all agent workspaces in the current directory.

    Scans subdirectories for cinna workspaces (.cinna/config.json), stops each
    sync session, and deletes the directories entirely.
    """
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    cwd = Path.cwd()
    agents: list[tuple[Path, object | None]] = []
    for child in sorted(cwd.iterdir()):
        if not child.is_dir():
            continue
        # The agent's config lives either directly in this dir (legacy flat) or
        # one level down in the Model-A nested layout (<clone>/<subdir>/.cinna/).
        cfg_dir = None
        if (child / ".cinna" / "config.json").is_file():
            cfg_dir = child
        else:
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and (
                    grandchild / ".cinna" / "config.json"
                ).is_file():
                    cfg_dir = grandchild
                    break
        if cfg_dir is None:
            continue
        # Delete the top-level dir (clone root for nested agents) on cleanup, but
        # load the config from wherever .cinna/ actually is.
        try:
            agents.append((child, load_config(cfg_dir)))
        except Exception:
            agents.append((child, None))

    if not agents:
        console.status("No cinna workspaces found in current directory.")
        return

    table = Table(
        title=f"Found {len(agents)} workspace{'s' if len(agents) != 1 else ''}",
        border_style="yellow",
        title_style="bold yellow",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Directory", style="bold")
    table.add_column("Agent")

    for i, (ws_dir, config) in enumerate(agents, 1):
        name = config.agent_name if config else "[dim]unknown[/dim]"
        table.add_row(str(i), f"{ws_dir.name}/", name)

    console.console.print()
    console.console.print(table)
    console.console.print()

    warning = Text()
    warning.append("  This will ", style="yellow")
    warning.append("stop all sync sessions", style="bold red")
    warning.append(" and ", style="yellow")
    warning.append("delete all directories", style="bold red")
    warning.append(" listed above.", style="yellow")
    console.console.print(
        Panel(
            warning,
            border_style="red",
            title="[bold red]Warning[/bold red]",
            padding=(0, 1),
        )
    )
    console.console.print()

    if not console.confirm("Are you sure?"):
        raise click.Abort()

    console.console.print()

    results: list[tuple[str, str, str]] = []  # (label, phase, result)

    with console.file_progress() as progress:
        task = progress.add_task("Cleaning up workspaces...", total=len(agents) * 2)

        for ws_dir, config in agents:
            label = config.agent_name if config else ws_dir.name

            progress.update(task, description=f"Stopping sync — {label}")
            if config is not None:
                try:
                    sync_session.stop(config)
                    remove_agent_registry(config.agent_id)
                    results.append((label, "Sync", "stopped"))
                except Exception as e:
                    results.append((label, "Sync", f"failed: {e}"))
            else:
                results.append((label, "Sync", "skipped (no config)"))
            progress.advance(task)

            progress.update(task, description=f"Deleting directory — {label}")
            try:
                shutil.rmtree(ws_dir)
                results.append((label, "Directory", "deleted"))
            except Exception as e:
                results.append((label, "Directory", f"failed: {e}"))
            progress.advance(task)

    log_file = cwd / "cinna.log"
    if log_file.exists():
        log_file.unlink()

    console.console.print()
    summary = Table(title="Results", border_style="green", title_style="bold green")
    summary.add_column("Agent", style="bold")
    summary.add_column("Action")
    summary.add_column("Result")

    for label, phase, result in results:
        if "failed" in result:
            result_styled = f"[red]{result}[/red]"
        else:
            result_styled = f"[green]{result}[/green]"
        summary.add_row(label, phase, result_styled)

    console.console.print(summary)
    console.console.print()
    console.status("All agent workspaces cleaned up.")


# ─── completion (unchanged) ────────────────────────────────────────────────


@cli.command()
@click.argument(
    "shell", required=False, type=click.Choice(["bash", "zsh", "fish"]), default=None
)
@click.option("--install", is_flag=True, help="Install completion to your shell config")
def completion(shell: str | None, install: bool):
    """Output shell completion script.

    \b
      cinna completion zsh          # print script to stdout
      cinna completion --install    # auto-detect shell and install
      eval "$(_CINNA_COMPLETE=zsh_source cinna)" # activate in current session
    """
    import subprocess as sp

    env_var = "_CINNA_COMPLETE"

    # Bare `cinna completion` (no shell, no --install): guide the user instead
    # of dumping the raw script to the terminal.
    if shell is None and not install:
        detected = _detect_shell()
        console.console.print("Shell completion for cinna. Pick one:\n")
        console.console.print(
            f"  cinna completion --install      install for your shell ({detected})"
        )
        console.console.print(
            "  cinna completion --install bash|zsh|fish   install for a specific shell"
        )
        console.console.print(
            f'  eval "$({env_var}={detected}_source cinna)"   enable in this session only'
        )
        console.console.print(
            f"  cinna completion {detected}             print the raw script (for piping)"
        )
        return

    if shell is None:
        shell = _detect_shell()

    source_cmd = f"{shell}_source"

    if install:
        result = sp.run(
            ["cinna"],
            capture_output=True,
            text=True,
            env={**os.environ, env_var: source_cmd},
        )
        script = result.stdout.strip()
        if not script:
            raise click.ClickException("Failed to generate completion script.")

        rc_file, snippet = _install_target(shell, script)
        rc = Path(rc_file).expanduser()

        if rc.exists() and "cinna completion" in rc.read_text():
            console.status(f"Completion already installed in {rc_file}")
            return

        with open(rc, "a") as f:
            f.write(f"\n# cinna CLI completion\n{snippet}\n")
        console.status(f"Completion installed in {rc_file}. Restart your shell or run:")
        console.console.print(f"  source {rc_file}")
    else:
        result = sp.run(
            ["cinna"],
            capture_output=True,
            text=True,
            env={**os.environ, env_var: source_cmd},
        )
        click.echo(result.stdout)


@cli.command(name="mcp-proxy", hidden=True)
def mcp_proxy():
    """Run MCP stdio server for knowledge queries. Called by Claude Code, not directly."""
    run_mcp_proxy()


def _detect_shell() -> str:
    """Detect current shell from SHELL env var."""
    shell_path = os.environ.get("SHELL", "")
    for name in ("zsh", "bash", "fish"):
        if name in shell_path:
            return name
    return "bash"


def _install_target(shell: str, script: str) -> tuple[str, str]:
    """Return (rc_file, snippet_to_append) for each shell type."""
    if shell == "zsh":
        # compdef (used by the generated script) only exists after compinit has
        # run. A bare zsh (the macOS default) hasn't loaded it, so guard first,
        # otherwise sourcing fails with "command not found: compdef".
        return "~/.zshrc", (
            "if ! type compdef &>/dev/null; then "
            "autoload -Uz compinit && compinit; fi\n"
            'eval "$(_CINNA_COMPLETE=zsh_source cinna)"'
        )
    elif shell == "fish":
        return (
            "~/.config/fish/completions/cinna.fish",
            script,
        )
    else:
        return "~/.bashrc", 'eval "$(_CINNA_COMPLETE=bash_source cinna)"'


def _default_machine_name() -> str:
    return f"{os.environ.get('USER', 'dev')}'s {platform.node()}"


def _resolve_machine_name(name: str | None) -> str:
    """``--name`` if given; else prompt on a real terminal, else the default.

    The TTY check keeps ``curl … | python3 -`` and piped spawns non-interactive
    even without ``--no-input``; with the flag the default is taken outright.
    """
    if name is not None:
        return name
    default_name = _default_machine_name()
    if console.interactive():
        return console.prompt("Machine name", default=default_name)
    return default_name
