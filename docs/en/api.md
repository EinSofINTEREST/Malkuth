# Control Plane API

**[한국어](../ko/api.md)** | English

The Control Plane is the one process that owns everything outside an agent container: the
catalog of declarations, the authoring surface that writes them, the deployments that turn a
graph into running containers, and the runs that drive those containers. The
[Web UI](ui.md) is a client of this API and nothing more — every screen is one of the calls
below.

```bash
python -m malkuth.orchestrator
```

It reads `configs/{MALKUTH_ENV}.yaml`. Which surfaces open depends on that file:

| Surface | Opens when |
|---|---|
| Runs (read, drain) | always |
| Catalog, authoring | always — `registry.roots` resolves against `MALKUTH_REPO_ROOT` |
| Deployments, run submission, resume | `orchestrator.deployment_store` is set |
| Web UI at `/ui` | always |

Without `orchestrator.deployment_store` the process refuses to start containers and answers
`POST /v1/runs/{id}/resume` with `501` — it reads the run store but does not drive runs.

## Authentication

**Authentication is on only when `orchestrator.control_token` is set.** With a token, every
`/v1/*` route requires it as a bearer token:

```bash
curl -H "Authorization: Bearer $MALKUTH_CONTROL_TOKEN" http://127.0.0.1:8700/v1/graphs
```

**Without a token the guard is off and every route is open.** That configuration is allowed
only on a loopback bind: the process refuses to start when it binds any other address without
a token (`CFG_001`), and it logs a warning when it starts without one. Set a token for
anything but a single-operator machine.

Two routes are unauthenticated by design even with a token set: `GET /v1/health` (Docker
healthchecks call it) and the static UI under `/ui` (the page holds no data of its own).

A missing or wrong token is `401` — and that response is **not** the error envelope below.
It is FastAPI's own `{"detail": "invalid control plane token"}` with a `WWW-Authenticate`
header, because the check runs as a dependency before any route.

## Errors

Failures raised by the control plane share one envelope, and the `code` is the stable part —
match on it rather than on the message. Two responses do **not** use it: `401` (above) and the
`501` from a control plane that drives no runs, which carries a flatter legacy shape.

```json
{
  "error": {
    "category": "not_found",
    "code": "NF_001",
    "message": "unknown graph: no-such-graph",
    "agent": null,
    "task_id": null,
    "retryable": false,
    "details": {"kind": "graph", "name": "no-such-graph"}
  }
}
```

Status codes follow one rule — **who can fix it**:

| Status | Meaning | Typical codes |
|---|---|---|
| `400` | the request is malformed or the declaration is invalid | `VAL_001`, `VAL_002`, `MOD_002`, `CFG_002` |
| `401` | missing or wrong token | — |
| `404` | no such resource | `NF_001` |
| `409` | the request conflicts with current state, and you can resolve it | `GRAPH_006`, `RT_010`, `RT_011`, `RT_012` |
| `500` | the server has to fix it | `RT_001`, `GRAPH_002`, `STOR_003` |
| `503` | not now, but worth retrying | anything with `retryable: true` |

`details` carries the specifics you need to act: which field failed, which version was
expected, which agent is already deployed.

## Health and UI

### `GET /v1/health`

Unauthenticated liveness. `{"status": "ok"}`.

### `GET /` → `GET /ui/`

`/` redirects to the operator UI. See [the UI guide](ui.md).

## Catalog

Read-only views of the declarations on disk. Every listing returns two arrays: `items` for
what parsed, and `problems` for what did not. **A broken file never hides the rest** — one
unparseable manifest would otherwise turn the whole catalog into a `500` with no clue which
file to fix.

### `GET /v1/agents`, `GET /v1/graphs`, `GET /v1/groups`

```json
{
  "items": [
    {
      "name": "planner",
      "version": "0.4.0",
      "group": "research",
      "description": "질의를 실행 가능한 리서치 계획으로 나누는 에이전트",
      "model": {"provider": "anthropic", "name": "claude-sonnet-5"}
    }
  ],
  "problems": []
}
```

Graph summaries carry `mode`, `goal`, and `nodes` instead of `model`; group summaries carry
`quotas`. A `problem` names the `path`, a `code`, and the message. Declarations (agents,
graphs, groups) report `VAL_002` for both kinds of failure — a schema violation and a name
that does not match its location. Module listings report what the registry raised instead
(`MOD_001`, `MOD_003`).

### `GET /v1/agents/{name}`, `GET /v1/graphs/{name}`, `GET /v1/groups/{name}`

The whole declaration as parsed — the same document the runtime will act on, not the file
text. `404` (`NF_001`) when the name is unknown.

### `GET /v1/modules/{module_type}`

`module_type` is `skillsets`, `promptsets`, or `memorysets`. Lists every published version:

```json
{"items": [{"name": "planner", "versions": ["0.1.0", "0.2.0", "0.3.0"]}], "problems": []}
```

### `GET /v1/modules/{module_type}/{name}/{version}`

One module document. Versions are exact — there is no `latest`.

## Authoring

Writes declarations to disk after validating them. Location is identity: a graph named
`docs-demo` lives at `graphs/docs-demo.yaml`, so the path segment and the declared name must
agree (`VAL_002` otherwise).

### `POST /v1/validate`

Validates drafts **without writing anything**. This is what the editor calls on every change.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graphs": [ ... ], "agents": [ ... ]}' http://127.0.0.1:8700/v1/validate
```

```json
{"ok": false, "findings": [
  {"check": "mode_rules", "code": "GRAPH_001",
   "message": "mission graph must be able to reach END", "graph": "docs-demo"}
]}
```

Findings are `200`, not an error status — an invalid draft is a normal answer to "is this
valid yet?". A body that is not a draft at all is `400` (`VAL_002`).

### `PUT /v1/graphs/{name}`, `PUT /v1/agents/{name}`

Validates and saves one declaration. Returns `{"name": ..., "path": ...}`.

Two rules the editor depends on:

- **Changing content requires a version bump.** Saving different content under the same
  version is `400` (`MOD_002`, with `existing` and `proposed` in `details`). Saving identical
  content is idempotent.
- **A declaration in use cannot be replaced.** If a running run or a live deployment
  references it, the save is refused (`VAL_002`, "agent is currently deployed — tear the
  deployment down first"). Tear the deployment down first.

Writes are atomic: the file is written beside its target and renamed, so a crash mid-write
cannot leave a truncated declaration.

### `PUT /v1/declarations`

Saves several declarations **as one unit** — validated together, and rolled back together if
any of them fails. Use it when a change spans files, such as adding a node and the agent it
points at:

```json
{"graphs": {"docs-bundle": { ... }}, "agents": {"echo": { ... }}}
```

```json
{"written": ["/repo/graphs/docs-bundle.yaml", "/repo/agents/echo/manifest.yaml"]}
```

### `DELETE /v1/graphs/{name}`, `DELETE /v1/agents/{name}`

`204` on success. Refused while something references the declaration (`400`, `VAL_002`), and
the `details` differ by reason: an agent still used by saved graphs lists them in
`referenced_by`, while anything currently deployed reports `kind` and `name` instead.

**Only the declaration is removed.** An agent that carries its own `Dockerfile` or `src/`
keeps them: the file the control plane wrote is the file it deletes, and code you wrote is
not the control plane's to throw away. The agent disappears from the catalog while that
directory stays on disk.

## Deployments

A deployment turns one graph into a set of running containers. The base image carries no
declarations, so the runtime mounts the manifest and the module roots read-only and injects
the A2A wiring the graph declares.

### `POST /v1/deployments`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graph": "research-pipeline"}' http://127.0.0.1:8700/v1/deployments
```

`201` once every agent reports healthy:

```json
{
  "deployment_id": "dep-2fc94a0b5f24",
  "graph": "research-pipeline",
  "version": "1.0.0",
  "status": "ready",
  "error": null,
  "updated_at": "2026-09-13T11:29:06.391594+00:00",
  "agents": [
    {"name": "planner", "replica": 0, "container_id": "0f2edbe1aa5a",
     "image": "malkuth/agent-base:0.1.0", "control_port": 32852, "a2a_port": 9100}
  ]
}
```

The call blocks until the agents are healthy or the deadline passes — a few seconds for the
reference graph. Per-agent tokens are never in a response.

`status` is one of:

| Status | Meaning |
|---|---|
| `starting` | containers are coming up |
| `ready` | every agent passed a health check and accepts tasks |
| `failed` | a step failed; **everything this deployment started was stopped again** |
| `stopped` | torn down through `DELETE` |
| `lost` | after a restart, at least one container was gone |

Failures leave no ghost containers. What you get back tells you which kind it was:

- `404` (`NF_001`) — no such graph.
- `400` (`VAL_001`) — the graph does not validate; nothing was started.
- `400` (`VAL_002`) — an agent with build materials declares a `runtime.image` other than the
  tag its build produces (`malkuth/agent-<name>:<version>`). Two places naming two images
  leave no way to tell what runs.
- `409` (`RT_012`) — an agent with build materials has no `built` image for its version: never
  built, the last build failed, or a build is still running. `details` carries `image`,
  `build_status` and `build_error`. Build it, then deploy again — a deploy never builds for
  you. Nothing was started and no deployment was recorded. Agents without materials run on
  the base image and are never gated.
- `409` (`RT_010`) — an agent of this graph is already running under another deployment. Tear
  that one down first; a graph and its agents deploy as a unit, so two live deployments
  cannot share an agent.
- `503` (`RT_002`) — an agent never became healthy before the deadline. Marked retryable:
  the containers were rolled back, so a second attempt is safe.

### `GET /v1/deployments`, `GET /v1/deployments/{id}`

The listing is `{"items": [...]}`; a single fetch is the record itself. Both reflect the
containers that are actually there right now — if supervision replaced a container after a
crash, the record shows the new id and port.

### `DELETE /v1/deployments/{id}`

Drains each agent, waits for in-flight tasks, then stops and removes the containers. Returns
the record with `status: "stopped"`; the record stays so the history remains readable. A
deployment already `stopped` or `failed` is returned unchanged.

## Runs

A run drives a deployed graph. Because the deployment knows where its agents are, submitting
a run takes a `deployment_id` — never an address.

### `POST /v1/runs`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"deployment_id": "dep-2fc94a0b5f24", "input": {"query": "..."}}' \
  http://127.0.0.1:8700/v1/runs
```

`202` immediately — a run can take minutes, and holding an HTTP request open for it helps
nobody:

```json
{"run_id": "run-a2bce8e90f40", "graph": "research-pipeline", "mode": "mission",
 "status": "running", "iteration": 0, "failure_streak": 0,
 "drain_requested": false, "updated_at": "..."}
```

`mode` comes from the graph, not from the caller. Optional fields: `run_id` to choose the id,
`mode` to assert the graph's mode (a mismatch is `400`).

Refusals: `404` (`NF_001`) when the deployment is unknown or not `ready`; `400` (`VAL_002`)
when the graph on disk has been edited to a different version than the one deployed —
redeploy before running, or the run would drive containers built from another declaration.

### `GET /v1/runs`, `GET /v1/runs/{run_id}`

The listing takes `?mode=mission|service`. A single run adds two fields once it has finished
**in this process**: `state` (the final graph state) and `error`. After a control plane
restart the record survives but that final state does not — durable state lives in the
checkpointer.

### `POST /v1/runs/{run_id}/drain`

Asks a run to stop after its current iteration and returns immediately with
`drain_requested: true`. The run reaches `stopped` on its own; poll `GET` to see it. Draining
is how a service run is stopped — there is no kill.

### `POST /v1/runs/{run_id}/resume`

Continues a run from where it stopped. What that means differs by mode:

- **Service runs** resume from their last iteration, and only from `halted` — the state a
  service graph reaches after too many consecutive failures (`GRAPH_005`). Any other state is
  `409` (`GRAPH_006`): a run you drained on purpose is submitted again, not resumed.
- **Mission runs** resume from the last checkpoint and carry **no state guard** — the
  checkpointer decides what continuing means, so resuming one that already finished re-drives
  it from that checkpoint. Without a durable checkpointer there is nothing to continue from
  (`STOR_002`).

`501` means this control plane has no deployment surface: it can read runs but does not drive
them, and answering `200` would leave an operator believing a resume happened.

## Operational notes

- **One agent, one deployment.** Two graphs that share an agent cannot be deployed at the
  same time (`409`, `RT_010`).
- **Editing is blocked while deployed.** This is deliberate: the containers were built from a
  specific declaration, and letting the file drift would make the deployment unreproducible.
- **Modifying a live system** means: deploy the new version, then tear the old one down.
  In-place patching of a running deployment is not supported.
- **Restarts reattach.** On startup the control plane matches its records against Docker and
  picks the containers back up, health monitoring included. Records whose containers are gone
  become `lost` rather than being deleted quietly.

## CLI equivalents

| API | CLI |
|---|---|
| `POST /v1/runs` | `malkuth run --deployment <id> --input '{...}'` |
| `GET /v1/runs` | `malkuth run-list [--mode service]` |
| `POST /v1/runs/{id}/drain` | `malkuth run-drain <id>` |
| `POST /v1/runs/{id}/resume` | `malkuth run-resume <id>` |

The run commands take `--control-url` and `--control-token` (or `MALKUTH_CONTROL_TOKEN`).

`malkuth validate` is deliberately absent from that table: it is a **local** command that
reads the repository directly and takes no control-plane flags. `POST /v1/validate` is the
remote equivalent, and it validates a draft you have not saved. See the
[root README](../../README.md#commands) for the full command reference.
