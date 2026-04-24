"""`cinna-sync-ssh` — SSH transport shim for Mutagen.

Mutagen invokes us with an argv that looks like:
    cinna-sync-ssh user@cinna-agent-<uuid> -- mutagen-agent <args…>

We translate that into a WebSocket to the platform's /sync-stream endpoint and
pump stdin/stdout both ways. The shim is stateless — each invocation opens a
fresh WebSocket and exits when either side closes.

Credential resolution (per invocation, keyed by argv agent_id):
    1. `~/.cinna/agents.json` registry — authoritative per-agent credentials,
       always re-read from disk so token rotations made by `cinna connect`
       take effect immediately, even if the Mutagen daemon was launched with
       a stale environment.
    2. Env-var fallback — only used when the registry has no entry for the
       agent_id. Guarded by a CINNA_AGENT_ID match so a daemon spawned for
       agent A never leaks its env to a later sync of agent B.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Sequence
from urllib.parse import urlparse


def _extract_agent_id(host: str) -> str | None:
    """Accept `user@cinna-agent-<id>` or just `cinna-agent-<id>`."""
    if "@" in host:
        host = host.split("@", 1)[1]
    prefix = "cinna-agent-"
    if host.startswith(prefix):
        return host[len(prefix):]
    return None


def _parse_argv(argv: Sequence[str]) -> tuple[str, list[str]]:
    """Return (host, remote_command_tokens).

    Mutagen's SSH invocation can look like any of these — OpenSSH-compatible:
      cinna-sync-ssh user@host -- mutagen-agent arg1 arg2
      cinna-sync-ssh user@host mutagen-agent arg1 arg2
      cinna-sync-ssh -p 22 user@host mutagen-agent arg1 arg2
    We accept anything reasonable and extract the host + tail command.
    """
    args = list(argv[1:])
    # Drop flags that take a value (e.g. -p 22, -i key). Conservative: strip
    # any token starting with `-` and the following token if it's a value.
    tokens: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            tokens.extend(args[i + 1:])
            i = len(args)
            break
        if tok.startswith("-") and tok not in ("-T", "-q"):
            # Two-token options OpenSSH uses that Mutagen may pass through.
            if tok in ("-p", "-i", "-o", "-l", "-F") and i + 1 < len(args):
                i += 2
                continue
            i += 1
            continue
        tokens.append(tok)
        i += 1

    if not tokens:
        sys.stderr.write("cinna-sync-ssh: no host in argv\n")
        sys.exit(2)

    host = tokens[0]
    remote = tokens[1:]
    return host, remote


def _ws_url(platform_url: str, agent_id: str) -> str:
    """Derive wss:// URL from the CLI's http(s) platform URL."""
    parsed = urlparse(platform_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    prefix = parsed.path.rstrip("/")
    return f"{scheme}://{parsed.netloc}{prefix}/api/v1/cli/agents/{agent_id}/sync-stream"


async def _run(ws_url: str, token: str, preamble: dict) -> int:
    try:
        import websockets
    except ImportError:
        sys.stderr.write(
            "cinna-sync-ssh: the 'websockets' package is required. "
            "Reinstall cinna-cli.\n"
        )
        return 1

    headers = [("Authorization", f"Bearer {token}")]
    try:
        connection = await websockets.connect(
            ws_url,
            additional_headers=headers,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
    except TypeError:
        # Older websockets releases used `extra_headers`.
        connection = await websockets.connect(
            ws_url,
            extra_headers=headers,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 — surface any connect failure
        sys.stderr.write(f"cinna-sync-ssh: connect failed: {exc}\n")
        return 1

    async with connection as ws:
        await ws.send(json.dumps(preamble).encode("utf-8"))

        loop = asyncio.get_running_loop()
        stdin_reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stdin_reader), sys.stdin.buffer
        )
        stdout_fd = sys.stdout.buffer

        async def stdin_to_ws() -> None:
            try:
                while True:
                    data = await stdin_reader.read(65536)
                    if not data:
                        break
                    await ws.send(data)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"cinna-sync-ssh: stdin pump ended: {exc}\n")

        async def ws_to_stdout() -> None:
            try:
                async for message in ws:
                    if isinstance(message, str):
                        stdout_fd.write(message.encode("utf-8"))
                    else:
                        stdout_fd.write(message)
                    stdout_fd.flush()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"cinna-sync-ssh: ws pump ended: {exc}\n")

        pump_stdin = asyncio.create_task(stdin_to_ws())
        pump_out = asyncio.create_task(ws_to_stdout())

        done, pending = await asyncio.wait(
            [pump_stdin, pump_out], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    return 0


def _resolve_credentials(agent_id: str) -> tuple[str, str]:
    """Resolve (cli_token, platform_url) for a given agent_id.

    Registry is preferred over env because:
      * the Mutagen daemon captures its env once at startup and never refreshes,
        so env can be arbitrarily stale after `cinna connect` rotates the token
        (the old env token would be revoked server-side and produce HTTP 403);
      * the registry file is re-read on every invocation and is rewritten by
        every `cinna connect`, so it is always the freshest view.

    Env is kept as a fallback for first-run edge cases (registry file missing
    entirely), gated by a CINNA_AGENT_ID match to prevent cross-agent leakage.
    """
    try:
        from cinna.config import lookup_agent_registry
    except ImportError as exc:
        sys.stderr.write(f"cinna-sync-ssh: cannot import cinna.config: {exc}\n")
        sys.exit(2)

    entry = lookup_agent_registry(agent_id)
    if entry:
        token = entry.get("cli_token")
        url = entry.get("platform_url")
        if token and url:
            return token, url
        sys.stderr.write(
            f"cinna-sync-ssh: incomplete registry entry for agent {agent_id}\n"
        )
        sys.exit(2)

    env_agent = os.environ.get("CINNA_AGENT_ID")
    env_token = os.environ.get("CINNA_CLI_TOKEN")
    env_url = os.environ.get("CINNA_PLATFORM_URL")
    if env_agent == agent_id and env_token and env_url:
        return env_token, env_url

    sys.stderr.write(
        f"cinna-sync-ssh: no registered credentials for agent {agent_id}. "
        "Run 'cinna connect' from the agent's workspace to register it.\n"
    )
    sys.exit(2)


def main() -> None:
    host, remote = _parse_argv(sys.argv)
    agent_id = _extract_agent_id(host)
    if not agent_id:
        sys.stderr.write(
            f"cinna-sync-ssh: unexpected host '{host}', expected cinna-agent-<uuid>\n"
        )
        sys.exit(2)

    if not remote:
        sys.stderr.write("cinna-sync-ssh: no remote command in argv\n")
        sys.exit(2)

    token, platform_url = _resolve_credentials(agent_id)

    ws_url = _ws_url(platform_url, agent_id)
    preamble = {"remote_command": remote}

    try:
        exit_code = asyncio.run(_run(ws_url, token, preamble))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
