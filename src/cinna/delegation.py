"""Durable delegation commands for account workspaces and cloud executor reports.

``cinna delegation create|status|report|reply`` drive the platform's durable
delegation contract: a delegated task carries ``delegation_metadata`` (a stable
identity derived from the requester's key), its executor reports a structured
result, and an open question is answered by its exact result id.

Account-workspace calls ride the account api-proxy and first negotiate the
contract version (``tasks/delegation-capabilities``). ``report`` without a
TASK_ID runs inside a cloud task session instead and posts with the session's
agent token.
"""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

import click
import httpx
from rich.markup import escape

from cinna import console
from cinna.errors import PlatformError

SUPPORTED_VERSION = 1

_CLOUD_ENV = ("AGENT_AUTH_TOKEN", "BACKEND_URL", "ENV_ID")


# ─── helpers ─────────────────────────────────────────────────────────────────


def _account_call(action):
    """Run ``action(client)`` against the account workspace's platform.

    Negotiates the contract first: a server without the capabilities route
    (404/405) or with a different contract version gets one clean message
    instead of a confusing failure further down. Other errors propagate.
    """
    from cinna.account import find_account_root, load_account_config
    from cinna.client import AccountClient

    config = load_account_config(find_account_root())
    with AccountClient(config) as client:
        try:
            capabilities = client.get_delegation_capabilities()
        except PlatformError as error:
            if error.status_code in (404, 405):
                raise _unsupported() from error
            raise
        if not isinstance(capabilities, dict) or (
            capabilities.get("version") != SUPPORTED_VERSION
        ):
            raise _unsupported()
        return action(client)


def _unsupported() -> click.ClickException:
    return click.ClickException("This server does not support durable delegations.")


def _text(value, fallback: str = "—") -> str:
    """A backend value made safe for a Rich print (missing → ``fallback``)."""
    if value is None or value == "":
        return fallback
    return escape(str(value))


def _emit_json(result) -> bool:
    """In ``--json`` mode emit the backend body under one key; True if done."""
    if console.json_mode:
        console.emit_result(delegation=result)
        return True
    return False


def _parse_artifacts(values: tuple[str, ...]) -> list[dict]:
    """Each ``--artifact`` must be a JSON object with kind, name and an
    HTTP(S) ``ref`` — the only kind of reference another agent can open."""
    artifacts = []
    for value in values:
        try:
            artifact = json.loads(value)
        except ValueError:
            artifact = None
        if not isinstance(artifact, dict):
            raise click.BadParameter(
                f"Not a JSON object: {value}", param_hint="--artifact"
            )
        missing = [
            key
            for key in ("kind", "name", "ref")
            if not isinstance(artifact.get(key), str) or not artifact[key].strip()
        ]
        if missing:
            raise click.BadParameter(
                f"Artifact needs non-empty string {', '.join(missing)}: {value}",
                param_hint="--artifact",
            )
        if urlparse(artifact["ref"]).scheme.lower() not in ("http", "https"):
            raise click.BadParameter(
                "Artifact ref must be an http(s) URL (upload local files first): "
                f"{artifact['ref']}",
                param_hint="--artifact",
            )
        artifacts.append(artifact)
    return artifacts


def _session_id_from_context() -> str:
    """The cloud session id from ``CINNA_SESSION_CONTEXT_PATH`` (or
    ``session_context.json`` in the cwd)."""
    context = Path(os.getenv("CINNA_SESSION_CONTEXT_PATH") or "session_context.json")
    try:
        data = json.loads(context.read_text())
    except (OSError, ValueError) as error:
        raise _no_session() from error
    session_id = data.get("backend_session_id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id.strip():
        raise _no_session()
    return session_id


def _no_session() -> click.ClickException:
    return click.ClickException("No current cloud task session is available.")


def _post_cloud_report(payload: dict) -> dict:
    """POST the report for the current cloud session with the agent token."""
    from cinna.client import AccountClient

    missing = [name for name in _CLOUD_ENV if not os.getenv(name)]
    if missing:
        raise click.UsageError(
            "Supply TASK_ID in an account workspace, or run inside an "
            f"authenticated cloud task session (missing: {', '.join(missing)})."
        )
    payload = {**payload, "source_session_id": _session_id_from_context()}
    backend = os.environ["BACKEND_URL"].rstrip("/")
    with console.spinner("Reporting..."):
        response = httpx.post(
            f"{backend}/api/v1/agent/tasks/current/delegation-result",
            headers={
                "Authorization": f"Bearer {os.environ['AGENT_AUTH_TOKEN']}",
                "X-Agent-Env-Id": os.environ["ENV_ID"],
            },
            json=payload,
            timeout=30,
            follow_redirects=False,
        )
    if not response.is_success:
        raise PlatformError(response.status_code, AccountClient.error_detail(response))
    # Accepted is what matters; a body we cannot read is not a failure.
    try:
        result = response.json() if response.content else {}
    except ValueError:
        result = {}
    return result if isinstance(result, dict) else {}


# ─── commands ────────────────────────────────────────────────────────────────


@click.group()
def delegation():
    """Create delegated work, report results, reply, and inspect status."""


@delegation.command("create")
@click.option(
    "--id",
    "requester_key",
    required=True,
    help="Stable requester key. Retrying with the same --id and --target reuses "
    "the original task; a changed --title or --brief is ignored.",
)
@click.option("--target", required=True, help="UUID of the agent that does the work.")
@click.option("--title", required=True, help="Short task title.")
@click.option("--brief", required=True, help="The full instructions for the target agent.")
@click.option("--group", default=None, help="Optional label grouping related delegations.")
@click.option(
    "--depth",
    type=click.IntRange(1, 2),
    default=1,
    show_default=True,
    help="Delegation depth: 1 for a top-level delegation, 2 for one made by a "
    "delegated agent (requires --root).",
)
@click.option(
    "--root",
    default=None,
    help="Delegation id of the depth-1 root; required with --depth 2, not allowed "
    "with --depth 1.",
)
@click.option(
    "--execute",
    is_flag=True,
    help="Start the new task; repeated creates never repeat execution.",
)
def create(requester_key, target, title, brief, group, depth, root, execute):
    """Delegate work to another agent as a durable task.

    Prints the task id that `status`, `report` and `reply` take. Safe to
    retry: the same --id and --target always resolve to the same task.
    """
    if depth == 1 and root:
        raise click.UsageError("--root is only allowed with --depth 2.")
    if depth == 2 and not root:
        raise click.UsageError("--depth 2 requires --root.")

    identity = json.dumps([target, requester_key])
    delegation_id = str(uuid5(NAMESPACE_URL, identity))
    metadata = {
        "id": delegation_id,
        "requester_key": requester_key,
        "origin_kind": "external",
        "origin_agent_id": None,
        "origin_chat_id": None,
        "origin_task_id": None,
        "depth": depth,
        "root": root or delegation_id,
        "group": group,
    }
    body = {
        "title": title,
        "original_message": brief,
        "selected_agent_id": target,
        "external_ref": hashlib.sha256(identity.encode()).hexdigest(),
        "delegation_metadata": metadata,
        "auto_execute": execute,
    }
    with console.spinner("Creating delegated task..."):
        result = _account_call(lambda client: client.create_delegated_task(body))
    if _emit_json(result):
        return

    created = result if isinstance(result, dict) else {}
    task_id = created.get("id")
    executes = created.get("auto_execute", execute)
    console.status(f"Delegated task [bold]{_text(task_id, 'unknown')}[/bold]")
    console.console.print(f"  Title:    {_text(created.get('title'), escape(title))}")
    console.console.print(f"  Status:   {_text(created.get('status'))}")
    console.console.print(
        f"  Executes: {'yes' if executes else 'no (not started; pass --execute to start it)'}"
    )
    if task_id:
        console.console.print()
        console.console.print(f"Check it with: cinna delegation status {escape(str(task_id))}")


@delegation.command("status")
@click.argument("task_id")
def status(task_id):
    """Read once. Results wake the requester; do not poll inside a running turn."""
    with console.spinner("Fetching delegation..."):
        result = _account_call(lambda client: client.get_task_detail(task_id))
    if _emit_json(result):
        return

    detail = result if isinstance(result, dict) else {}
    outcome = detail.get("delegation_result")
    outcome = outcome if isinstance(outcome, dict) else {}
    title = detail.get("title")
    heading = f"Task [bold]{_text(detail.get('id'), escape(task_id))}[/bold]"
    if title:
        heading += f" — {escape(str(title))}"
    console.console.print(heading)
    console.console.print(f"  State:    {_text(detail.get('status'))}")
    if not outcome:
        console.console.print("  Result:   none reported yet")
        return
    console.console.print(
        f"  Result:   {_text(outcome.get('status'))} — {_text(outcome.get('summary'))}"
    )
    question = outcome.get("question")
    if outcome.get("status") == "blocked" and question:
        console.console.print(f"  Question: {escape(str(question))}")
        console.console.print(f"  Audience: {_text(outcome.get('audience'))}")
        console.console.print(f"  Result id: {_text(outcome.get('id'))}")
        if outcome.get("reply_state"):
            console.console.print(f"  Reply:    {_text(outcome.get('reply_state'))}")
        elif outcome.get("id"):
            console.console.print()
            console.console.print(
                f"Answer with: cinna delegation reply {escape(task_id)} "
                f"--result-id {escape(str(outcome['id']))} --message '...'"
            )


@delegation.command("report")
@click.argument("task_id", required=False)
@click.option(
    "--status",
    "result_status",
    required=True,
    type=click.Choice(["in_progress", "blocked", "done", "failed"]),
    help="Where the work stands; blocked requires --question.",
)
@click.option("--summary", required=True, help="One-line summary of the result.")
@click.option("--question", default=None, help="The question that blocks the work.")
@click.option(
    "--audience",
    type=click.Choice(["requester", "user"]),
    default="requester",
    show_default=True,
    help="Who should answer the question: the requesting agent or a person.",
)
@click.option(
    "--artifact",
    multiple=True,
    help='JSON object {"kind", "name", "ref"} with an http(s) ref; repeatable.',
)
@click.option("--body", default="", help="The full result text.")
def report(task_id, result_status, summary, question, audience, artifact, body):
    """Report a task, or the current authenticated cloud session when TASK_ID is omitted."""
    if result_status == "blocked" and not (question or "").strip():
        raise click.UsageError("--status blocked requires --question.")
    artifacts = _parse_artifacts(artifact)
    payload = {
        "status": result_status,
        "summary": summary,
        "question": question,
        "audience": audience,
        "artifacts": artifacts,
        "body": body,
    }
    if task_id:
        with console.spinner("Reporting..."):
            result = _account_call(
                lambda client: client.put_delegation_result(task_id, payload)
            )
    else:
        result = _post_cloud_report(payload)
    if _emit_json(result):
        return

    target = f"task {escape(task_id)}" if task_id else "the current task"
    message = f"Reported [bold]{escape(result_status)}[/bold] for {target}"
    result_id = result.get("id") if isinstance(result, dict) else None
    if result_id:
        message += f" (result {escape(str(result_id))})"
    console.status(message)


@delegation.command("reply")
@click.argument("task_id")
@click.option(
    "--result-id",
    required=True,
    help="Exact result id of the open question, as shown by status.",
)
@click.option("--message", required=True, help="The answer to deliver.")
def reply(task_id, result_id, message):
    """Answer a blocked delegation's open question.

    The reply is addressed to one exact result id, so an answer never lands on
    a newer question; resending the same answer is safe.
    """
    with console.spinner("Sending reply..."):
        result = _account_call(
            lambda client: client.post_delegation_reply(task_id, result_id, message)
        )
    if _emit_json(result):
        return

    outcome = result if isinstance(result, dict) else {}
    if outcome.get("delivered"):
        console.status(f"Reply delivered to task {escape(task_id)}")
    elif outcome.get("uncertain"):
        console.warn(
            "Reply is being delivered but not yet confirmed; check "
            f"`cinna delegation status {escape(task_id)}` before sending again."
        )
    else:
        console.warn(
            f"Reply not delivered: result {escape(result_id)} is not an open question."
        )
