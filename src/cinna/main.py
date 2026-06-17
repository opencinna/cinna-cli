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
from cinna.mcp_proxy import run_mcp_proxy
from cinna.mutagen_runtime import ensure_mutagen_ready

logger = logging.getLogger("cinna.exec")


@click.group()
@click.version_option(version=__version__)
@click.option("-v", "--verbose", is_flag=True, help="Show debug logs in terminal")
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

    if name is None:
        default_name = _default_machine_name()
        if sys.stdin.isatty():
            name = click.prompt("Machine name", default=default_name)
        else:
            name = default_name

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

    if name is None:
        default_name = _default_machine_name()
        if sys.stdin.isatty():
            name = click.prompt("Machine name", default=default_name)
        else:
            name = default_name

    run_set_token(" ".join(setup_input), name)


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
    help="Directory to create the account workspace in "
    "(default: the platform domain, e.g. demo-core_opencinna_io; "
    "you'll be prompted to accept or change it)",
)
def account_setup(setup_input: tuple[str, ...], name: str | None, dir_name: str | None):
    """Set up an account workspace from an account setup token.

    Accepts any of these formats (paste directly from Settings → Local
    Development):

    \b
      cinna account setup curl -sL http://host/api/cli-setup/account/TOKEN | python3 -
      cinna account setup http://host/api/cli-setup/account/TOKEN
      cinna account setup TOKEN
    """
    from cinna.account import run_account_setup

    if name is None:
        default_name = _default_machine_name()
        if sys.stdin.isatty():
            name = click.prompt("Machine name", default=default_name)
        else:
            name = default_name

    run_account_setup(" ".join(setup_input), name, dir_name)


@account.command(name="agents")
@click.option(
    "--all", "show_all", is_flag=True,
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
def account_status():
    """Show account workspace info and account token validity."""
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
def account_credentials_list(workspace: str | None):
    """List your credentials with their setup status (metadata only)."""
    from cinna.account import run_credentials_list

    run_credentials_list(workspace)


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
@click.option("--service-uri", default=None, help="New non-secret audience / target URL")
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


@agent.command(name="restart-env")
@click.argument("agent_ref")
def agent_restart_env(agent_ref: str):
    """Restart AGENT_REF's environment (recover a stuck env / poisoned API).

    The first-class recovery path: bounces the agent's container without the
    raw API escape hatch. Blocks until the env is back, then prints its status.
    Use this when a producer's REST API is stuck reporting an old error, or the
    env is otherwise wedged.
    """
    from cinna.account import run_agent_restart_env

    run_agent_restart_env(agent_ref)


@agent.command(name="show")
@click.argument("agent_ref")
@click.option(
    "--prompts", "prompts_only", is_flag=True,
    help="Show only the effective prompts (skip features + credentials)",
)
@click.option(
    "--full", "full", is_flag=True,
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
@click.option("--tz", "timezone", default="UTC", help="IANA timezone for interpretation (default UTC)")
@click.option(
    "--type", "schedule_type",
    type=click.Choice(["static_prompt", "script_trigger"]),
    default="static_prompt",
    help="Which minimum-interval floor applies to the generated cadence",
)
def agent_schedule_generate(agent_ref: str, text: str, timezone: str, schedule_type: str):
    """Preview a CRON string from natural-language TEXT (nothing is saved).

    Example: cinna agent schedule generate crm-agent "every weekday at 7am" --tz Europe/Berlin
    """
    from cinna.account import run_schedule_generate

    run_schedule_generate(agent_ref, text, timezone, schedule_type)


@agent_schedule.command(name="create")
@click.argument("agent_ref")
@click.option("--name", required=True, help="Schedule name")
@click.option("--cron", required=True, help="CRON expression in --tz local time (5 fields)")
@click.option("--tz", "timezone", default="UTC", help="IANA timezone for the cron (default UTC)")
@click.option(
    "--type", "schedule_type",
    type=click.Choice(["static_prompt", "script_trigger"]),
    default="static_prompt",
    help="static_prompt (always starts a session) or script_trigger (runs a command)",
)
@click.option("--prompt", default=None, help="Per-schedule prompt (static_prompt only)")
@click.option("--command", default=None, help="Shell command (required for script_trigger)")
@click.option("--description", default=None, help="Human description (defaults to the name)")
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
        agent_ref, name, cron, timezone, schedule_type,
        prompt, command, description, enabled=not disabled,
    )


@agent_schedule.command(name="update")
@click.argument("agent_ref")
@click.argument("schedule_id")
@click.option("--enable/--disable", "enabled", default=None, help="Enable or disable the schedule")
@click.option("--name", default=None, help="New name")
@click.option("--cron", default=None, help="New CRON expression (requires --tz)")
@click.option("--tz", "timezone", default=None, help="IANA timezone (required when --cron changes)")
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
        agent_ref, schedule_id, enabled, name, cron, timezone,
        prompt, command, description,
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
@click.option("--producer", "producer_ref", required=True, help="Agent exposing the REST API (name, slug, or id)")
@click.option("--consumer", "consumer_ref", required=True, help="Agent that will call it (name, slug, or id)")
@click.option("--label", default=None, help="Label for the created credential")
@click.option("--read-only", is_flag=True, help="Restrict the consumer to read-only API access")
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
@click.option("--producer", "producer_ref", required=True, help="Agent exposing an agent2agent MCP connector (name, slug, or id)")
@click.option("--consumer", "consumer_ref", required=True, help="Agent that will consume it (name, slug, or id)")
@click.option("--label", default=None, help="Label for the created credential")
@click.option("--conversation-only", is_flag=True, help="Enable the connection in conversation mode only")
@click.option("--building-only", is_flag=True, help="Enable the connection in building mode only")
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
@click.option("--disable", is_flag=True, help="Disable the REST API instead of enabling it")
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
    "--output", "-o", default=None,
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
    "--method", "-X", default="GET",
    type=click.Choice(
        ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        case_sensitive=False,
    ),
    help="HTTP method (default GET)",
)
@click.option("--query", "query_pairs", multiple=True, help="Query param key=value (repeatable)")
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


# ─── api (escape hatch) ────────────────────────────────────────────────────


@cli.command(name="api")
@click.argument("method", type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE"], case_sensitive=False))
@click.argument("path")
@click.option("--json", "json_text", default=None, help="Inline JSON request body")
@click.option("--data", "data_file", default=None, help="JSON request body from a file (@file.json)")
@click.option("--query", "query_pairs", multiple=True, help="Query parameter as key=value (repeatable)")
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
                            stream, nbytes, first_delta_at - started_at,
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
        config.agent_id, exec_id, exit_code, duration,
        stdout_bytes, stderr_bytes, terminal_event,
    )
    return exit_code


# ─── status ────────────────────────────────────────────────────────────────


@cli.command()
def status():
    """Show agent info and current sync state."""
    root = find_workspace_root()
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
    that no longer exist are flagged as missing (they can be cleaned up with
    ``cinna disconnect`` from the parent directory).
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
        ensure_mutagen_ready(client, config, root, interactive=sys.stdin.isatty())

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
                console.status(
                    f"Local versions backed up to {res.backup_dir}"
                )
        else:
            console.status("No conflicts — local workspace is consistent with remote.")
        if res.remaining:
            console.warn(
                f"{len(res.remaining)} conflict(s) could not be auto-resolved — "
                "resolve them in the Conflicts tab."
            )
        console.status("Attaching live view. Press Ctrl-C to stop.")
    else:
        console.status(f"Sync session created ({st.state}) — attaching live view. Press Ctrl-C to stop.")

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
                        logger.error("remote rm error for %s: %s", relpath, event.get("content"))
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
@click.option("--agent", "agent_ref", default=None, help="Target a synced agent from the account root")
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
@click.option("--agent", "agent_ref", default=None, help="Target a synced agent from the account root")
def sync_conflicts(agent_ref: str | None):
    """List sync conflicts the Mutagen daemon has parked.

    Sources from the daemon's conflict list (authoritative), so this agrees
    with the count shown by ``cinna sync status`` — two-way-safe does not write
    ``.conflict.*`` files on disk, so a disk walk would always look empty.
    """
    _root, config = _resolve_sync_target(agent_ref)

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
    console.console.print(
        "\nResolve with 'cinna sync resolve --prefer local' (your local edits "
        "win) or '--prefer remote' (the container's version wins)."
    )


@sync.command("push")
@click.option("--agent", "agent_ref", default=None, help="Target a synced agent from the account root")
@click.option("--force", is_flag=True, help="Local wins: clear conflicts in favor of local before flushing")
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
                config, root, prefer="local",
                remote_delete=_make_remote_deleter(config),
            )
        if res.resolved:
            console.status(f"Resolved {len(res.resolved)} conflict(s) in favor of local.")
        if res.remaining:
            console.warn(f"{len(res.remaining)} conflict(s) could not be resolved: {', '.join(res.remaining)}")

    # `mutagen sync flush` is bidirectional; the push/pull distinction is the
    # --force resolution direction, not the flush itself.
    with console.spinner("Flushing sync…"):
        st = sync_session.flush(config)
    console.status(f"Sync settled ({st.state}).")
    if st.conflict_count:
        console.warn(
            f"{st.conflict_count} conflict(s) remain — your edits are NOT fully live. "
            "Re-run with --force (local wins) or 'cinna sync resolve'."
        )


@sync.command("pull")
@click.option("--agent", "agent_ref", default=None, help="Target a synced agent from the account root")
@click.option("--force", is_flag=True, help="Remote wins: clear conflicts in favor of remote before flushing")
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
            console.status(f"Resolved {len(res.resolved)} conflict(s) in favor of remote.")
            if res.backup_dir is not None:
                console.status(f"Local versions backed up to {res.backup_dir}")
        if res.remaining:
            console.warn(f"{len(res.remaining)} conflict(s) could not be resolved: {', '.join(res.remaining)}")

    # `mutagen sync flush` is bidirectional; the push/pull distinction is the
    # --force resolution direction, not the flush itself.
    with console.spinner("Flushing sync…"):
        st = sync_session.flush(config)
    console.status(f"Sync settled ({st.state}).")
    if st.conflict_count:
        console.warn(
            f"{st.conflict_count} conflict(s) remain. "
            "Re-run with --force (remote wins) or 'cinna sync resolve'."
        )


@sync.command("resolve")
@click.option("--prefer", type=click.Choice(["local", "remote"]), required=True, help="Which side wins")
@click.option("--agent", "agent_ref", default=None, help="Target a synced agent from the account root")
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
        console.status(f"Resolved {len(res.resolved)} conflict(s) in favor of {prefer}.")
        if res.backup_dir is not None:
            console.status(f"Local versions backed up to {res.backup_dir}")
    else:
        console.status("No conflicts to resolve.")
    if res.remaining:
        console.warn(
            f"{len(res.remaining)} conflict(s) could not be resolved: "
            f"{', '.join(res.remaining)}"
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
    if not click.confirm("Continue?"):
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
        if child.is_dir() and (child / ".cinna" / "config.json").is_file():
            try:
                agents.append((child, load_config(child)))
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

    if not click.confirm("Are you sure?"):
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
      eval "$(cinna completion zsh)" # activate in current session
    """
    import subprocess as sp

    if shell is None:
        shell = _detect_shell()

    env_var = "_CINNA_COMPLETE"
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
        return "~/.zshrc", 'eval "$(_CINNA_COMPLETE=zsh_source cinna)"'
    elif shell == "fish":
        return (
            "~/.config/fish/completions/cinna.fish",
            script,
        )
    else:
        return "~/.bashrc", 'eval "$(_CINNA_COMPLETE=bash_source cinna)"'


def _default_machine_name() -> str:
    return f"{os.environ.get('USER', 'dev')}'s {platform.node()}"
