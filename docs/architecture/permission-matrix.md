# Permission matrix

Agents do not run tools. `MissionScheduler` dispatches a task to the one runner,
and the runner's only way to a handler is `app.tools.executor.execute_tool`.
That function denies a name that is not registered, checks the agent's grants
again, and for `external` or `dangerous` consumes a stored approval whose hash
matches this exact call. A prompt cannot widen any of this.

An approval row records who decided (`decided_by`), when (`decided_at`), the
decision, the SHA-256 of the tool name plus canonical params (`params_hash`),
and the mission id. It expires 15 minutes after it is created
(`JARVIS_APPROVAL_TTL_SECONDS`) and can be consumed once. Execution recomputes
the hash from the call it is about to run. A different argument, a second use,
or an expiry is a denial, and the handler is not called.

Canonical params drop keys that start with `_` (those are injected by the
executor after the check) and sort object keys. List order is part of the action.

## Levels

| Level | Meaning | Runs when |
|---|---|---|
| `read` | Look at state that already exists | The agent is granted the capability and lists the tool |
| `internal_write` | Change state that stays inside Jarvis or on a node the operator owns | Same. No approval |
| `external` | Send anything off this machine | Same, plus a matching unexpired single-use approval |
| `dangerous` | Hard to undo, or run code the model produced | Same, plus a matching unexpired single-use approval |

The older `SAFE` / `SENSITIVE` / `DANGEROUS` label is still stored. It is what
the drill in `drills/cases/drill_08.py` talks about. `web.fetch` and
`web.search` stay `SAFE` on that label because they do not mutate local state,
and they are `external` here because they leave the machine. Both gates have
to agree before a handler runs: an agent with the grant, and a human approval
when the policy level requires one.

## Tools

| Tool | Capability | Policy | Approval |
|---|---|---|---|
| `memory.search` | READ | read | no |
| `memory.read` | READ | read | no |
| `memory.write` | WRITE | internal_write | no |
| `memory.forget` | WRITE | dangerous | yes |
| `web.fetch` | READ | external | yes |
| `web.search` | READ | external | yes |
| `fs.list` | READ | read | no |
| `fs.read` | READ | read | no |
| `fs.write` | WRITE | internal_write | no |
| `fs.delete` | WRITE | dangerous | yes |
| `system.stats` | READ | read | no |
| `shell.exec` | EXECUTE | dangerous | yes |

`fs.write` is a write on the operator's own node, reversible by writing again,
so it is `internal_write` rather than `external`. `shell.exec` and `fs.delete`
are `dangerous` because one is arbitrary code and the other does not come back.

Node tools still run on a node. The executor only sends them after the policy
check. A missing node is reported. It is not run on the orchestrator instead.

## What is not an agent call

`POST /api/v1/nodes/dispatch` is the signed-in operator running a node tool
from the API, not an agent. It does not go through `execute_tool`. The node
still enforces its path jail and shell allowlist. Agent-originated calls do
not have this shortcut: there is no tool whose handler starts another agent,
and an agent id is not a registered tool name.
