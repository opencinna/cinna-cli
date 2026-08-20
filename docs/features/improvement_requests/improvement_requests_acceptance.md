# Improvement Requests — Acceptance Scenarios

Real-usage scenarios for `cinna improve`, run against a **live platform** with a
real account workspace. Unit tests (`tests/test_improve.py`) cover rendering and
transport; these scenarios cover what only a live backend can show: consent flow
end-to-end, recipient resolution for a bundle, authorization boundaries, and the
archive's actual contents.

## Preconditions

- A live platform URL where the Agent Improvement Requests feature is deployed
  (backend routes `/api/v1/cli/account/improvement-requests*`).
- An **editable install** of this repo: `which cinna` resolves into the repo's
  `src/cinna`.
- An account workspace: `cinna login <domain>` (or `cinna account setup <token>`)
  in an empty folder, then `cd` into it.
- **Agent A** — a standalone agent the account owns, with a conversation session
  containing at least a couple of messages.
- **Agent B** *(for the bundle scenarios)* — a bundle published from this account
  (the **publisher install**), plus a **consumer install** of that bundle owned by
  a *second* user account, with a session on it.
- Browser access to the platform UI as both users, to submit requests and to check
  the Configuration-tab card.

## Scenario catalog

### 1. Standalone agent — submit, list, show

**Goal** — the basic loop on an agent the account owns itself.
**Setup** — Agent A with a session that has ≥ 1 message.
**Steps**

```bash
# In the platform UI, as the owner: open the session → ⋮ → Improve Agent,
# write a comment ("it answered with last month's numbers"), confirm.
cinna improve list
cinna improve list --status new
cinna improve show <short-id>
```

**Expected** — the request appears with Agent A's name, the owner as requester,
`standalone` in the Version column, the comment text, and status `new`. `show`
prints the comment verbatim, the message count, and a runtime-context table
carrying session mode, engine, effective model, and the environment name.
**Watch for** — a self-targeted request that fails to resolve a recipient; a
context block that is empty or missing the effective model (the platform must use
its own resolver, not a re-implementation); a short id in the table that `show`
cannot resolve.

### 2. Submission via the `/session-improve` command

**Goal** — the command entry point produces the same row as the menu.
**Setup** — a second session on Agent A.
**Steps**

```bash
cinna chat --agent <agent-a> "give me the quarterly totals"
# In the platform UI, in that same session: /session-improve it used the wrong quarter
cinna improve list --agent <agent-a> --json
```

**Expected** — a second row for Agent A whose `source` is `command` and whose
`comment` is the text typed after the slash command.
**Watch for** — `source` reported as `web_ui` for a command submission; the
command's confirmation naming the wrong recipient.

### 3. Bundle install — the request lands on the publisher

**Goal** — the cross-user path: a consumer's session reaches the publisher.
**Setup** — the consumer account has an install of the published bundle and a
session on it.
**Steps**

```bash
# As the CONSUMER, in the platform UI: session ⋮ → Improve Agent → confirm.
# The modal must name the publisher and the bundle version before the button.
# Then, as the PUBLISHER, in the account workspace:
cinna improve list --status new
cinna improve show <short-id>
```

**Expected** — the row's target agent is the **publisher install**, the requester
is the consumer, and Version shows the installed version plus the bundle id.
`show` reports `consumer install`, the installed vs. latest version, and whether
an update was pending.
**Watch for** — the request landing on the consumer's own copy instead of the
publisher's; the publisher seeing the consumer's *other* sessions anywhere; a
`fallback_reason` set when the publisher install actually exists.

### 4. Download, extract, and read the archive

**Goal** — the archive is a valid, complete, self-describing package.
**Steps**

```bash
cinna improve download <short-id>
ls -R improvements/<short-id>/
head -40 improvements/<short-id>/README.md
python3 -c "import json;print(json.load(open('improvements/<short-id>/context.json'))['sdk'])"
```

**Expected** — `README.md`, `metadata.json`, `context.json`, and
`session/messages.md` + `session/messages.json` land under
`improvements/<short-id>/`; the command lists each extracted file, prints the
"another person's conversation" warning, and `context.json` carries the bundle
version, installed/latest revision, session mode, SDK engine, and effective model.
`README.md` states that container logs and uploaded file contents are excluded.
**Watch for** — a truncated or unreadable ZIP; `session/messages.md` missing the
tool calls that explain the failure; any uploaded file's *contents* (rather than a
descriptor) appearing in the archive.

### 5. The snapshot is frozen

**Goal** — post-consent conversation never leaks into an already-shared request.
**Steps**

```bash
cinna improve download <short-id> --out /tmp/before-<short-id>
cinna chat --agent <agent-a> --resume <session_id> "and now add the discounts"
cinna improve download <short-id>
diff -r /tmp/before-<short-id> improvements/<short-id>/
```

**Expected** — the two extractions are identical; the follow-up turn is absent
from both. The second run reports "Refreshed" rather than "Extracted".
**Watch for** — the archive growing after the session continues; a re-download
leaving a mix of old and new files behind.

### 6. Status transitions and the requester-visible note

**Goal** — the loop closes and the requester sees the outcome.
**Steps**

```bash
cinna improve status <short-id> in_progress
cinna improve list --status in_progress
cinna improve status <short-id> completed --note "Fixed in v1.6 — it no longer re-asks for an uploaded file."
cinna improve show <short-id>
```

**Expected** — each transition is confirmed, `show` reports the new status, a
status-changed timestamp, and the resolution note; the requester sees the note on
their side in the platform UI.
**Watch for** — a note silently dropped; `status_changed_at` not moving; the
Configuration-tab card not updating live (its WebSocket event).

### 7. Refusals — unknown status, unknown id, ambiguous id

**Goal** — fail-loud before the network, and never guess an id.
**Steps**

```bash
cinna improve status <short-id> wontfix
cinna improve show deadbeef
cinna improve show <2-char-prefix-shared-by-two-requests>
```

**Expected** — respectively: "Unknown status 'wontfix'" listing the four valid
values; "No improvement request matching 'deadbeef'" pointing at
`cinna improve list`; an "ambiguous" error listing the matching short ids. All
exit non-zero and change nothing.
**Watch for** — a prefix silently resolving to the first match; a bad status
reaching the backend as a 400/422 instead of a readable CLI error.

### 8. Authorization boundary — a non-recipient sees nothing

**Goal** — ids the account is not party to do not confirm their own existence.
**Setup** — the consumer account's own account workspace, and a request id taken
from the publisher's listing.
**Steps**

```bash
# As the CONSUMER (the requester, not the recipient):
cinna improve list
cinna improve status <publisher-request-id> completed
```

**Expected** — the consumer's listing does **not** contain the request (that
surface is the recipient's queue); the `status` attempt fails with the platform's
403 for a party requester, and with a 404 for a completely unrelated account.
**Watch for** — a 403 where a 404 is required for a stranger (existence leak); a
requester being able to mutate status or delete the row.

### 9. `--agent` filter and cross-agent scope

**Goal** — one queue across every owned agent, narrowable to one.
**Steps**

```bash
cinna improve list                      # both agents' requests
cinna improve list --agent <agent-a>
cinna improve list --agent "Nonexistent Agent"
```

**Expected** — the unfiltered listing spans both agents; the filtered one shows
only Agent A's; an unknown reference fails with the accessible-agent list from
`cinna account agents`.
**Watch for** — the filter being applied client-side (it must reach the backend as
`agent_id`); an agent name resolving to the wrong id when two agents share a slug.

### 10. Session deleted after submission

**Goal** — the payload is the row, not a pointer to a live session.
**Steps**

```bash
# Delete the source session in the platform UI, then:
cinna improve show <short-id>
cinna improve download <short-id> --out /tmp/after-delete
```

**Expected** — both still succeed with the same content; the request row survives
with its provenance link cleared.
**Watch for** — a 500 or an empty archive after the session is gone; the archive
degrading to a "snapshot unavailable" README when the snapshot is actually intact.

### 11. Truncated snapshot is disclosed

**Goal** — a capped snapshot is never read as a complete one.
**Setup** — a session long enough to exceed the platform's snapshot cap.
**Steps**

```bash
cinna improve show <short-id>
grep -i truncat improvements/<short-id>/README.md
```

**Expected** — `show` flags the message count as truncated (oldest messages
dropped), and the archive's README says the same.
**Watch for** — truncation dropping the *newest* messages (defects cluster at the
end); the flag present in the row but absent from the README.

## Cross-cutting invariants

- Every verb refuses to run outside an account workspace, before any network call.
- No verb ever creates a request — consent happens only in the platform UI or via
  `/session-improve`, on the requester's own session.
- No credential value, container log, or uploaded file's contents appears in any
  archive; scrubbing and exclusion are server-side and must stay that way.
- Extraction never writes outside the destination directory, whatever the archive
  claims.
- Ids the account is not party to return 404, never 403 — no existence leak.
- Downloaded archives stay under `improvements/` (or `--out`) — never inside
  `agents/<slug>/workspace/`, and never committed.
- The account token is the only credential involved; nothing is persisted to the
  agent registry.

## Cleanup

```bash
rm -rf improvements/ /tmp/before-* /tmp/after-delete
```

Close any request left `in_progress` (`cinna improve status <id> completed --note
"…"` or `declined`), delete the throwaway sessions in the platform UI, and — if
the scenarios created a test bundle install on the second account — uninstall it.
