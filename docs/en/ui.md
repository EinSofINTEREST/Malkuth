# Web UI

**[한국어](../ko/ui.md)** | English

The Control Plane serves an operator UI at `/ui`. It exists so that assembling, deploying,
and running an agent system is a thing you do in a browser rather than a sequence of `curl`
calls. There is no build step and no second process — the page ships with the package and
talks to the [Control Plane API](api.md) over `fetch`, nothing else. It never touches the
filesystem or Docker; anything the UI can do, the API can do.

```bash
python -m malkuth.orchestrator      # serves both the API and the UI
```

Open `http://127.0.0.1:8700/` — `/` redirects to `/ui/`.

## Connecting

The page loads without a token, but it cannot read anything until you paste
`orchestrator.control_token` into the **토큰** field and press **연결**. The indicator next to
it reports the result:

| Indicator | Meaning |
|---|---|
| 미연결 | no token has been submitted yet |
| 연결됨 | the token works; catalog, deployments, and runs have loaded |
| 토큰 거부 | the token is missing or wrong (`401`) |
| 연결 실패 | the control plane is unreachable |

The token is kept in `sessionStorage`, so it survives a reload and disappears when the tab
closes. It is never written to disk.

## The five tabs

### 카탈로그 — what you can build with

Four lists: agents, graphs, groups, and modules. Selecting an entry shows the parsed
declaration in full.

Declarations that failed to parse appear in a separate list at the bottom with their file
path and error code, rather than being silently missing. That distinction matters: an empty
agent list means "none declared", while a problem entry means "this file needs fixing".

### 그래프 편집기 — wiring

A form, not a canvas. The metadata block covers name, version, description, mode, goal, and
the state schema reference; three tables below it hold the moving parts:

- **노드** — an id plus the agent it binds to. The agent comes from a dropdown of the
  catalog, so a node cannot point at something that does not exist.
- **엣지** — `from`, `to`, an optional condition (an importable reference such as
  `malkuth.graphs.conditions:needs_research`), and `max_iterations` for cycles.
- **connections** — the A2A allowlist. Declaring `caller`/`callee` is what permits one agent
  to delegate to another at runtime; without it the call is refused.

Choosing `service` mode reveals the idle policy fields — a service graph without backoff
would spin the model in a busy loop, so the fields are required for that mode.

**검증** sends the draft to `POST /v1/validate` and lists every finding with its check name
and code. **저장** validates first and refuses to write if anything fails, so an invalid
graph never reaches disk through the UI.

**카탈로그에서 불러오기** loads an existing graph and bumps its patch version, because saving
changed content under the same version is refused — the version is how a deployment stays
reproducible.

### 에이전트 편집기 — manifests

The same shape for agent manifests: metadata and group, the model, module references, and the
runtime block (image, `env_allowlist`, whether A2A is exposed).

Module references are dropdowns built from the catalog's published versions. There is no free
text and no `latest` — every reference is pinned to a version that exists.

### 배포 — containers

Pick a graph, press **배포**, and the row appears with each agent's container id, ports, and
the deployment status (`starting` → `ready`, or `failed` with the reason). **해체** drains
and stops the containers; the record stays visible as `stopped`.

Two refusals show up here often, and both are the system protecting a live deployment:

- Deploying a graph whose agents already run under another deployment is refused (`409`).
  Tear that deployment down first.
- Saving an agent or graph that a live deployment uses is refused in the editors. To change a
  deployed system: deploy the new version, then tear the old one down.

### Run — driving it

Pick a ready deployment, give the initial state as JSON, and submit. The submission returns
immediately with a `run_id`; the table polls until the run finishes and then shows the final
state.

Rows carry the controls that apply to their state: **보기** for the full record, **drain**
for a running service run (it stops after the current iteration), **재개** for a halted one.

## The full loop

Everything the main goal asks for is these five tabs in order:

1. **카탈로그** — see which agents and modules exist.
2. **에이전트 편집기** — add or adjust an agent; save.
3. **그래프 편집기** — place the nodes, wire the edges, declare the A2A connections; validate
   until clean; save.
4. **배포** — deploy the graph and watch the agents turn healthy.
5. **Run** — submit input, watch the result.

Destroying is the same path in reverse: tear the deployment down, then delete the graph or
agent (deletion is refused while anything still references it).

## What the UI deliberately does not do

- **No drag-and-drop canvas.** Wiring is a table. A canvas needs a frontend toolchain, which
  is a dependency decision that has not been made yet (issue #252).
- **No in-place edit of a running system.** Every change goes through save → deploy → tear
  down, so what runs always matches a declaration on disk.
- **No secrets.** The UI never shows agent tokens or secret values. Secrets reach containers
  from the control plane's environment through `env_allowlist`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 토큰 거부 after entering a token | it does not match `orchestrator.control_token` |
| 배포 tab shows nothing and deploy fails | `orchestrator.deployment_store` is not configured, so the deployment surface is closed |
| Save refused with a version message | the content changed but the version did not — bump it |
| Save refused with "currently deployed" | tear the deployment down first |
| A run ends immediately with `GRAPH_002` | an agent could not run the node; open its container logs |

For the exact status codes and payloads behind each screen, see the
[Control Plane API](api.md).
