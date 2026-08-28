# Malkuth

**[한국어](docs/ko/README.md)** | English

A modular multi-agent orchestration framework built on LangGraph.

Malkuth composes each goal as a graph of **equal, Docker-isolated agents**. Agents carry
their own A2A endpoints and MCP servers, connect and disconnect through config-driven
wiring, and share nothing except the graph state and declared, scoped memory.

## Key Features

- **LangGraph orchestration** — graph topology declared in YAML, built into a
  `StateGraph` with checkpointing, resume, and conditional routing
- **One agent = one Docker container** — controlled through a standard Agent Control API,
  with resource limits, health checks, and graceful drain
- **Per-agent protocol isolation** — every A2A endpoint and MCP server belongs to exactly
  one agent; no shared tool gateways
- **Equal peers, three access paths** — orchestrated runs, interactive direct requests to
  any agent, and declared A2A peer calls; no hierarchy between agents
- **Two execution modes** — goal-oriented **mission** graphs that run to completion, and
  perpetual **service** graphs that iterate indefinitely with idle backoff
- **Everything is a module** — skillsets, promptsets, memorysets, and graphs are
  versioned, swappable deliverables; a solution is assembled, not written from scratch
- **Scoped resources** — secrets, memory, artifacts, and quotas managed at three scopes:
  **global / group / local**, resolved nearest-first
- **Context memory** — per-scope memory spaces with hybrid (vector + lexical) search
  indexes, token-budgeted recall, and declared retention

## Status

**Pre-v0.1.0.** The framework layers described above are implemented and covered by the
test suite; the control plane REST API, remote module registry, and Kubernetes runtime
backend remain future work.

## Requirements

Python 3.12+ · [uv](https://docs.astral.sh/uv/) · Docker Engine 24+ (for the runtime,
integration, and E2E paths)

```bash
uv sync --frozen        # or: make install
```

## Commands

### Quality gates

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (strict on malkuth.core)
make test               # unit tests + coverage gate (>= 70%)
make check              # lint + typecheck + test — run this before pushing
```

Integration and E2E suites are opt-in because they need Docker:

```bash
make test-integration   # marked `integration` — Docker containers, real MCP sessions
make test-e2e           # marked `e2e` — full compose stack (nightly in CI)
```

Checkpointer integration tests skip unless their backends are addressable:

```bash
export MALKUTH_TEST_POSTGRES_URL=postgresql://malkuth:malkuth@127.0.0.1:15432/malkuth
export MALKUTH_TEST_REDIS_URL=redis://127.0.0.1:16379    # needs RediSearch (redis-stack)
```

### Images and stacks

```bash
make build              # malkuth/agent-base + agent-echo + agent-claude-code images
make up / make down     # dev stack — one echo agent, control port on 18080
make e2e-up             # E2E stack — fake provider, memory service, 4 reference agents
make e2e-down
```

Run `make build` first. The compose files extend `malkuth/agent-base` but do not build it,
so bringing a stack up without rebuilding validates a stale image
([#222](https://github.com/EinSofINTEREST/Malkuth/issues/222)).

The E2E stack publishes agent control ports on **18081-18084**, the Memory Service on
**18090**, the checkpoint Postgres on **15433**, agent metrics on **19082-19084**, and A2A
ports on **19102-19104**. Its agents talk to a fake model provider, so nothing reaches a
real LLM.

### CLI

```bash
uv run malkuth <command>          # or `malkuth` inside an activated venv
```

| Command | What it does |
|---|---|
| `malkuth validate` | validate every graph, agent manifest, and module ref in the repo |
| `malkuth deploy <graph.yaml>` | validate one graph as a deploy gate (`--a2a-port-range`) |
| `malkuth status` | summarize declared agents, graphs, groups, and modules |
| `malkuth config [env]` | print the resolved configuration (`dev` / `staging` / `prod`) |
| `malkuth check <state.yaml>` | report integrity discrepancies against observed state |
| `malkuth run <graph.yaml>` | submit a mission or service run |
| `malkuth run-list` / `run-drain <id>` / `run-resume <id>` | operate runs through a control plane |

`--json` (before the subcommand) switches to machine-readable output; `--root` points at a
repository other than the working directory.

Running a graph needs the address of each agent's Control API — the orchestrator never
guesses where an agent lives. Against the E2E stack:

```bash
export MALKUTH_AGENT_TOKEN=e2e-token

# mission run — terminates at END and prints the final state
uv run malkuth run graphs/research-pipeline.yaml \
  --input '{"query": "malkuth architecture"}' \
  --agent planner=http://127.0.0.1:18082 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent writer=http://127.0.0.1:18084

# service run — perpetual loop, bounded here so it terminates
uv run malkuth run graphs/feed-monitor.yaml --service --iterations 2 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent planner=http://127.0.0.1:18082 \
  --agent writer=http://127.0.0.1:18084
```

A service run without `--iterations` runs until interrupted; `Ctrl-C` requests a drain, so
it stops after finishing the current iteration rather than mid-flight.

Two limits are worth knowing before you rely on them: `--checkpointer` currently only
works as `memory`, because the CLI has no way to supply a connection URL for `postgres`
or `redis` ([#220](https://github.com/EinSofINTEREST/Malkuth/issues/220)) — durable
checkpointing is reachable through the library API meanwhile. And `run-list` /
`run-drain` / `run-resume` need a control plane process that is not shipped yet
([#221](https://github.com/EinSofINTEREST/Malkuth/issues/221)).

### Long-running processes

```bash
python -m malkuth.agentd     # in-container agent daemon — Control API on 8080
python -m malkuth.memory     # Memory Service — HTTP surface plus the async indexing loop
```

`agentd` is what the runtime layer starts inside every agent container; it reads
`MALKUTH_MANIFEST`, `MALKUTH_AGENT_TOKEN`, `MALKUTH_ROOT`, and — when memory is wired —
`MALKUTH_MEMORY_URL` with `MALKUTH_MEMORY_TOKEN` or `MALKUTH_MEMORY_TOKEN_FILE`.

The Memory Service reads `MALKUTH_REPO_ROOT`, `MALKUTH_MEMORY_PORT`, and
`MALKUTH_MEMORY_TOKENS_PATH`. It must run as its own process: appends commit immediately
but indexing is asynchronous, so without the loop nothing becomes searchable.

Both honour `MALKUTH_LOG_LEVEL`, `MALKUTH_LOG_FORMAT`, and `MALKUTH_METRICS_PORT`.

### Configuration

Configuration lives in `configs/{env}.yaml` (`dev`, `staging`, `prod`). The CLI selects it
positionally; the long-running processes read `MALKUTH_ENV`:

```bash
uv run malkuth config prod            # print the resolved prod configuration
```

Overrides use a **double underscore** to separate section from key:

```bash
MALKUTH_ORCHESTRATOR__NODE_TIMEOUT_S=600 uv run malkuth config
```

Single-underscore `MALKUTH_*` variables are process settings (`MALKUTH_AGENT_TOKEN` and
the like), never configuration — the loader ignores them on purpose, so that injecting
agent env into a container cannot corrupt that container's configuration.

For a walkthrough that assembles a solution from scratch, see
[Getting Started](docs/en/getting-started.md).

## Documentation

| Document | Description |
|---|---|
| [Architecture](docs/en/architecture.md) | Layers, interaction model, execution modes, resource scoping |
| [Getting Started](docs/en/getting-started.md) | Environment setup and first solution |
| [Module System](docs/en/modules.md) | Skillsets, promptsets, memorysets, graphs, groups |
| [Testing](docs/en/testing.md) | Test strategy and quality gates |
| [CI Conventions](docs/en/ci/conventions.md) | Merge gates, workflow design rules |
| [Required Status Checks](docs/en/ci/status-checks.md) | Single source of truth for check names |

Development rules (the authoritative ruleset) live in [.claude/rules/](.claude/rules/README.md).

## Technology Stack

Python 3.12+ · uv · LangGraph · pydantic v2 · Docker · `a2a-sdk` · `mcp` ·
FastAPI · structlog · Prometheus · pytest

## Conventions (Summary)

- Commit messages: `[FEAT]: ...` / `[FIX]: ...` / `[REFAC]: ...` / `[DOCS]: ...` /
  `[CHORE]: ...` — written in Korean
- PR titles: `[FEAT#N] title` (category + issue number)
- Branches: `{category}/#{issue}/{summary}`
- Documentation: English first (`docs/en/`), Korean mirror (`docs/ko/`)

See [.claude/rules/07-code-style.md](.claude/rules/07-code-style.md) and
[.claude/rules/08-workflow.md](.claude/rules/08-workflow.md) for the full conventions.
