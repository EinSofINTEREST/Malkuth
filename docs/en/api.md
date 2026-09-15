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
| Build materials | `orchestrator.material_store` is set — otherwise the routes answer `400` (`CFG_001`) |
| Image builds, and the deploy gate on custom agents | `orchestrator.material_store` **and** `orchestrator.build_store` are set — otherwise the image routes are absent (`404`) |
| Access registry | `orchestrator.access_store` is set — otherwise the routes are absent (`404`) |
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

Because a name becomes a file path, every route that takes an agent, graph or group name
checks it first: lowercase letters, digits and single hyphens only. Anything else, such as
`..`, a slash or an uppercase letter, is `400` (`VAL_002`) and nothing on disk is read, written
or deleted. The same holds for reads, material routes and image routes.

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

**Only the declaration is removed.** The manifest file goes; the agent's directory under
`agents/` stays, and so do its build materials in the material store. Materials stay bound
to their version: if you declare the same name and version again, you get those materials
back, and different materials under that version are still refused (`MOD_002`). Bump the
version to start over.

## Build materials and images

A custom agent is an agent with **build materials**: a `Dockerfile` (optional) and a `src/`
tree. They live in the control plane's material store, keyed by the agent's name and its
declared version, not in the repository. Declarative agents have none and never build: they
run on the base image with their declarations mounted.

Building is an explicit step. Saving materials does not build, and deploying does not build
either — a deploy **refuses** a custom agent whose image for that version is not `built`
(`409`, `RT_012`; see [`POST /v1/deployments`](#post-v1deployments)).

### `GET /v1/agents/{name}/materials`

The materials for the agent's **current** version. An agent without any is not an error:

```json
{"agent": "docs-custom", "version": "0.1.0", "files": {}, "updated_at": ""}
```

`404` (`NF_001`) if the agent is not declared.

### `PUT /v1/agents/{name}/materials`

Stores the materials for the agent's current version. The body maps context-relative paths
to text content:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"files": {"src/marker.py": "MARK = 1\n"}}' \
  http://127.0.0.1:8700/v1/agents/docs-custom/materials
```

The response has the same shape as `GET`, with `updated_at` stamped. The rules:

- **Paths** are `Dockerfile` or under `src/`, relative, posix-style, and already normalised
  (`src/./a.py` and `src//a.py` are refused, as is anything with `..`). At most 200 files,
  each at most 256 KiB of text. Violations are `400` (`VAL_002`) with the offending `path`
  in `details`.
- **A version's materials are immutable.** Different content under the same version is `400`
  (`MOD_002`); identical content is idempotent. Bump the agent's version, save the agent,
  then save the new materials.
- **Materials of a deployed agent cannot change** (`400`, `VAL_002`).

The `Dockerfile` is checked when you build, not when you save.

### `DELETE /v1/agents/{name}/materials`

`204`. Clears the materials of the current version, so the agent runs as a declarative agent
again. The version stays taken: saving different materials under it afterwards is still
`MOD_002`. Refused while the agent is deployed.

### `POST /v1/agents/{name}/image`

Submits a build and returns at once with `202`:

```json
{"agent": "docs-custom", "version": "0.1.0", "status": "building",
 "image": "malkuth/agent-docs-custom:0.1.0", "error": null, "log": "",
 "updated_at": "2026-09-14T11:02:03.123456+00:00"}
```

The builder assembles a temporary build context from the catalog and the store, bakes it as
`malkuth/agent-<name>:<version>`, and deletes the directory:

```
Dockerfile        # yours, or the skeleton when you saved none
manifest.yaml     # the agent's declaration
modules/          # the module roots
src/              # your materials
```

The skeleton copies those three into `/app` and puts `/app/src` on `PYTHONPATH`, so a manifest
`spec.entrypoint: agent:MyAgent` resolves to `src/agent.py`. A `Dockerfile` of your own must:

- start `FROM malkuth/agent-base:<tag>` — the base carries `agentd`;
- not end as `root` (going up to install and back down is fine);
- `COPY`/`ADD` only from inside the context — no absolute paths, no `..`, no remote URLs.

What is refused before anything is built:

- `404` (`NF_001`) — the agent is not declared.
- `400` (`VAL_002`) — the agent has no materials, or the `Dockerfile` breaks one of the rules
  above.
- `409` (`RT_011`) — a build of this version is already running. Two builds would write the
  same tag, and the one that finished last would win.

A build that starts and then fails is **not** an HTTP error. It ends as `failed` in the
record below. `error` only says which image failed; the cause is in `log`, the tail of
Docker's output:

```
Step 2/2 : RUN exit 3
 ---> Running in 80c5a8ac55e7
The command '/bin/sh -c exit 3' returned a non-zero code: 3
```

### `GET /v1/agents/{name}/image`

The build record for the agent's current version, plus whether it needs one:

```json
{"agent": "docs-custom", "version": "0.1.2", "status": "failed",
 "image": "malkuth/agent-docs-custom:0.1.2", "needs_build": true,
 "error": "image build failed: malkuth/agent-docs-custom:0.1.2",
 "log": "Step 1/2 : FROM malkuth/agent-base:0.1.0\n ... returned a non-zero code: 3",
 "updated_at": "..."}
```

| `status` | Meaning |
|---|---|
| `null` | never built |
| `building` | a build is running — poll again |
| `built` | the image exists; a deploy may use it |
| `failed` | the last build failed — read `error` and `log` |

`needs_build` is `false` for an agent without materials. `log` keeps the last 8000 characters.
Records survive a control plane restart.

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

## Access registry

Permissions that change while agents run are decided **outside the agent's container, per
request** — an agent may hold every permission inside its own container, so a check there is
a convenience, not a boundary. The registry is where those decisions are made; the enforcement
points (Memory Service, egress proxy, a callee's A2A server) ask it and cache the answer.

The surface opens when `orchestrator.access_store` is set:

```yaml
orchestrator:
  access_store: ./var/access.db          # identities, grants, revocations — survives restarts
  access_enforcer_token: ${ENFORCER}     # must differ from control_token
  access_agent_url: http://control-plane:8700   # the control plane as agent containers reach it
  access_stewards: [permission-agent]    # the only agents that may grant
```

Binding a non-loopback address with `access_store` but no `access_enforcer_token` refuses to
start (`CFG_001`), for the same reason the control token is required there. `access_store`
without `access_agent_url` also refuses to start (`CFG_001`): agents need it to prove who they
are on A2A calls, and without it they would fall back to a signing key every agent in the graph
holds.

**Enforcement is being rolled out point by point.** Memory and A2A calls are enforced today —
the Memory Service in registry mode and each callee's A2A server ask for every request (see
[Memory enforcement](#memory-enforcement) and [A2A enforcement](#a2a-enforcement)). Egress and
remote MCP tools are recorded here but not yet enforced, so a decision for those kinds changes
nothing the agent can reach.

### Three callers, three credentials

One token for everyone would hand the enforcement points operator powers, and the permission
agent the ability to read every decision. So each audience authenticates as itself:

| Caller | Credential | Routes |
|---|---|---|
| Operator | control plane token | read records, revoke, lift |
| Permission agent | **its own agent identity** | grant |
| Any agent (A2A) | **its own agent identity** | call tickets, verify a received ticket, change feed |
| Enforcement point | `access_enforcer_token` | decide, identities, change feed |

### Agent identity

Every deployment issues one identity per agent and injects it as
`MALKUTH_ACCESS_CREDENTIAL`. The registry stores only its SHA-256 hash; the value appears in no
API response. Identities survive control plane restarts, are re-injected when supervision
replaces a container, and stop working the moment the deployment is torn down or rolled back.

### How a request is decided

In order, first match wins:

1. an active operator **revocation** covering the request → `deny`
2. the agent's **declaration** allows it → `allow`, `decided_by: "declaration"`
3. an active **grant** covering it → `allow`, `decided_by: <permission agent>`
4. otherwise → `deny`, `decided_by: "default"`

A revocation therefore beats both the declaration and any grant. For memory, revoking with
`mode: "rw"` removes writing only — reading stays.

Step 2 counts only for a resource kind whose enforcement point is in place — today, `memory` and
`a2a`. For `a2a` the declaration is the `connections` of the graph the caller is **currently
deployed in**; a graph on disk that is not deployed grants nothing.
Each kind's declaration check is wired together with the enforcement point that uses it, so the
declaration and its enforcement cannot drift apart. Until then, a declaration contributes
nothing for that kind, and a request with no grant is `deny` with `decided_by: "default"`.

Declarations are read on every decision, so a change takes effect without a restart. The
registry watches the agent manifests and group files and moves its version when one changes,
which makes enforcement points drop cached decisions. It also moves the version once when it
starts, because it cannot know what changed while it was down.

### Expansion ceilings

A permission agent can widen an agent's permissions only inside the **ceiling** the operator
declared for that agent's group or for `global`. The registry enforces it; the permission
agent's judgement is not the limit, because the request it acts on is untrusted input.

```yaml
# groups/research.yaml
spec:
  access:
    ceiling:
      max_ttl_s: 3600                                   # every grant expires, at most this late
      memory:
        - {space: "group:research:knowledge", mode: ro}
      egress: [api.search.example.com]
      mcp_tool: []
      a2a: []
```

A group without a ceiling gets no expansion. When the group's ceiling and `global` both
cover a request, the smaller `max_ttl_s` applies. Memory spaces must be explicit persistent
space ids (`local|group|global:<owner>:<alias>`), and no target may be blank or contain a
wildcard. Changing a ceiling is a declaration change; a permission agent cannot change its own.

### `POST /v1/access/grants` — permission agent

```bash
curl -X POST -H "Authorization: Bearer $MALKUTH_ACCESS_CREDENTIAL" \
  -H 'content-type: application/json' \
  -d '{"agent": "researcher", "kind": "egress", "target": "api.search.example.com",
       "ttl_s": 600, "reason": "fetch sources for run r-12", "requested_by": "researcher"}' \
  http://127.0.0.1:8700/v1/access/grants
```

`kind` is `memory`, `egress`, `mcp_tool` or `a2a`; `mode` (`ro`/`rw`) is required for memory
and must be absent otherwise. A mode that does not fit the kind is `400` (`VAL_002`) on every
access route, including revocations and decisions. `201` returns the record:

```json
{
  "rule_id": "rule-7c1e0a9b2d3f4e5a", "agent": "researcher", "kind": "egress",
  "target": "api.search.example.com", "mode": null, "effect": "allow",
  "decided_by": "permission-agent", "requested_by": "researcher",
  "reason": "fetch sources for run r-12",
  "created_at": 1789381200.0, "expires_at": 1789381800.0, "lifted_at": null
}
```

A refusal is `403` with `ACC_003`, and it is logged and counted
(`malkuth_access_grants_total{op="refuse"}`). It is refused when the caller is not listed in
`access_stewards`, grants to itself, names a memory permission without a mode, exceeds the
ceiling or its `max_ttl_s`, or asks for something an operator revoked — only an operator
undoes a revocation. An unknown or revoked identity is `403` with `ACC_001`; an unknown agent
is `404`.

### `POST /v1/access/decisions` — enforcement point

```json
{"credential": "<the identity presented to the enforcement point>",
 "kind": "egress", "target": "api.search.example.com"}
```

```json
{"agent": "researcher", "decision": "allow", "decided_by": "permission-agent", "version": 42,
 "valid_until": 1789381800.0}
```

`valid_until` is the earliest expiry among the records behind this decision, or `null`.
Expiry does not move the registry version, so an enforcement point must not use a cached
decision past `valid_until` — its own clock tells it when, even while the registry is
unreachable. Memory decisions require `mode`.

An unknown or revoked identity is not an error: it is answered `200` with
`{"agent": null, "decision": "deny", "decided_by": "unknown-identity"}`, so the enforcement
point can cache the refusal like any other answer.

### `POST /v1/access/identities` — enforcement point

```json
{"credential": "<the identity presented to the enforcement point>"}
```

```json
{"agent": "researcher", "version": 42}
```

An enforcement point that must resolve a name before it can ask for a decision — the Memory
Service turns an alias into a space id through the agent's declarations — first learns who is
asking. An unknown or revoked identity is `200` with `"agent": null`, cacheable like a denial.

### `GET /v1/access/changes?after=<version>&wait_s=<seconds>` — enforcement point or agent

Long-poll. Answers `{"version": N}` as soon as the registry version moves past `after`, or when
`wait_s` (at most 30) passes. Every grant, revocation and lift moves the version, and so does
tearing down a deployment (its identities stop working); an enforcement point drops its cache
when it sees a new one. The bearer is `access_enforcer_token` or a live agent identity — a callee
follows the feed to drop cached A2A decisions, and the feed carries only version numbers.

### `GET /v1/access/agents/{name}` — operator

The agent's records — active, expired and lifted — with the current `version`. Records are
never deleted, so this is the history of who decided what and why.

### `POST /v1/access/revocations` — operator

```json
{"agent": "researcher", "kind": "memory", "target": "group:research:knowledge",
 "mode": "rw", "reason": "incident 311", "expires_in_s": 3600}
```

`201` with the record, `effect: "deny"`, `decided_by: "operator"`. `expires_in_s` is optional;
without it the revocation lasts until lifted.

### `DELETE /v1/access/rules/{rule_id}` — operator

Lifts a revocation or ends a grant early. The record stays, with `lifted_at` set. `404`
(`NF_001`) for an unknown id.

### Memory enforcement

The Memory Service switches to **registry mode** when both of these are set in its environment
(one without the other refuses to start, `CFG_001`):

| Variable | Value |
|---|---|
| `MALKUTH_ACCESS_URL` | the control plane, reachable from the Memory Service |
| `MALKUTH_ACCESS_ENFORCER_TOKEN` | the control plane's `access_enforcer_token` |

In registry mode:

- **No memory tokens are issued.** An agent presents the identity its deployment injected;
  the control plane passes it as `MALKUTH_MEMORY_TOKEN` when a registry is configured, and never
  falls back to a static token.
- **Every request is decided.** The service resolves the alias through the agent's current
  declarations, then asks for `ro` (read, search, latest) or `rw` (append) on that space id.
  A denial is the usual `401` with `MEM_001`, and the audit log records it.
- **A revocation applies to the next request** of a running agent, and so does a `rw`→`ro`
  demotion — reads keep working, writes stop. Nothing restarts.
- **Searching without naming spaces skips spaces that are not allowed now**, rather than failing
  the whole search. `GET /v1/spaces` lists what is allowed, with the mode actually permitted.
- **The identity outlives a Memory Service restart.** It lives in the control plane.
- **While the registry is unreachable**, decisions already cached keep working and anything not
  yet decided is denied. A cached decision is still dropped at its `valid_until`.

The service follows the change feed while it runs. When the feed is lost but the registry still
answers, cached decisions older than two seconds are asked again.

**Turn both sides on together.** A control plane with `access_store` injects identities as memory
tokens; a Memory Service still in token mode does not know them and refuses every agent. The
reverse — a registry-mode Memory Service with a control plane that has no registry — refuses
every agent too, because nothing issues identities.

### A2A enforcement

Without a registry, a callee checks an HMAC token signed with a key the runtime gives **every
agent in the graph** — any of them can mint a token claiming to be any other. With a registry,
the callee decides each call itself:

1. The caller asks the registry for a **ticket** for one callee, authenticating with its own
   identity. The ticket lives five minutes and is reused until thirty seconds before it expires.
2. The caller sends the ticket in `x-malkuth-a2a-ticket`. Its identity never leaves for the callee.
3. The callee's A2A server sends the ticket to the registry, authenticating with **its own**
   identity. The registry refuses a ticket issued for another callee, an expired one, and one
   whose caller identity was revoked; otherwise it decides `a2a` for (caller, callee).
4. A denial — or no decision while the registry is unreachable and nothing is cached — is
   `A2A_004`, before the task reaches the agent. The callee caches decisions, follows the change
   feed with its own identity, and never keeps one past the ticket's expiry.

What this gives you:

- **Revoking a connection applies to the next call** of a running caller; lifting it restores
  calls without a redeploy. Nothing restarts.
- **Skipping the caller's own check does not help.** A caller that calls a port directly with an
  edge token, with another agent's name, or without a ticket is refused by the callee.
- **A ticket is useless elsewhere.** It is not an identity: the Memory Service does not accept it,
  and another agent rejects it because it names a different callee.

The caller still checks the declared connections first — it spares a round trip and gives a clear
`A2A_004` locally — but that check is a convenience, not the boundary.

### `POST /v1/access/a2a/tickets` — any agent

```json
{"callee": "planner"}
```

`201` with `{"ticket": "...", "callee": "planner", "expires_at": 1789381500.0}`. A ticket is proof
of identity, not permission: it is issued even for a revoked connection, and the callee refuses
the call. An unknown or revoked identity is `403` (`ACC_001`); an unknown callee is `404`.

### `POST /v1/access/a2a/verify` — the callee

```json
{"ticket": "<the ticket the caller sent>"}
```

```json
{"agent": "researcher", "decision": "allow", "decided_by": "declaration", "version": 42,
 "valid_until": 1789381500.0}
```

A ticket that is not for this callee, expired, forged, or from a revoked caller is `200` with
`"agent": null, "decision": "deny", "decided_by": "invalid-ticket"`. The bearer must be a live
agent identity — `403` (`ACC_001`) otherwise.

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
| `PUT /v1/agents/{name}/materials` | `malkuth agent-push <name> <directory>` |
| `POST` + `GET /v1/agents/{name}/image` | `malkuth agent-build <name> [--wait]` |

These commands take `--control-url` and `--control-token` (or `MALKUTH_CONTROL_TOKEN`).
`agent-push` reads a directory, skips cache directories such as `__pycache__`, and refuses
symbolic links. `agent-build --wait` exits non-zero when the build fails and prints the log
tail.

`malkuth validate` is deliberately absent from that table: it is a **local** command that
reads the repository directly and takes no control-plane flags. `POST /v1/validate` is the
remote equivalent, and it validates a draft you have not saved. See the
[root README](../../README.md#commands) for the full command reference.
