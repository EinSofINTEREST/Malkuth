# Getting Started

**[한국어](../ko/getting-started.md)** | English

> **Bootstrap notice**: Malkuth is in its pre-v0.1.0 bootstrap phase — the ruleset and
> documentation exist, but the framework code has not landed yet. Commands marked
> *(planned)* describe the contract the implementation will fulfill.

## Prerequisites

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) (package manager — lockfile is committed)
- Docker Engine **24+** (agent isolation runtime)
- `make` (task automation)

## Setup

```bash
git clone https://github.com/EinSofINTEREST/Malkuth.git
cd Malkuth
uv sync                 # (planned) install pinned dependencies
```

Quality gates used locally and in CI:

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (strict on src/malkuth/core)
make test               # pytest unit tests + coverage gate (≥ 70%)
make test-integration   # Docker-based integration tests
```

## Assembling a First Solution

A solution is **assembled from modules** — you wire existing agents and modules into a
graph instead of writing framework code.

### 1. Declare an agent

```yaml
# agents/researcher/manifest.yaml
apiVersion: malkuth/v1
kind: Agent
metadata:
  name: researcher
  version: 0.1.0
  group: research               # resource scope membership (optional)
spec:
  model: {provider: anthropic, name: claude-sonnet-5}
  promptset: {ref: promptsets/researcher@0.1.0}
  skillsets:
    - ref: skillsets/web-search@0.2.0
  memory:
    spaces:
      - {ref: memorysets/agent-longterm@0.1.0, as: longterm}
  a2a: {enabled: true}
  runtime:
    resources: {cpu: "1.0", memory: 1Gi}
    env_allowlist: [ANTHROPIC_API_KEY]
```

### 2. Wire a graph (the goal)

```yaml
# graphs/research-pipeline.yaml
apiVersion: malkuth/v1
kind: Graph
metadata: {name: research-pipeline, version: 1.0.0}
spec:
  mode: mission                 # mission (terminating) | service (perpetual)
  goal: answer a query with a researched report
  nodes:
    - {id: planner, agent: agents/planner@0.1.0}
    - {id: researcher, agent: agents/researcher@0.1.0}
    - {id: writer, agent: agents/writer@0.1.0}
  edges:
    - {from: START, to: planner}
    - {from: planner, to: researcher}
    - {from: researcher, to: writer}
    - {from: writer, to: END}
  connections:                  # A2A peer-call allowlist (equal peers)
    - {caller: researcher, callee: planner}
```

### 3. Declare group resources (optional)

```yaml
# groups/research.yaml
apiVersion: malkuth/v1
kind: Group
metadata: {name: research}
spec:
  quotas: {cpu: "8.0", memory: 16Gi, max_agents: 10}
  secrets: [SEARCH_API_KEY]
  memory:
    spaces:
      - {ref: memorysets/domain-knowledge@0.1.0, as: knowledge, mode: rw}
```

### 4. Deploy and run *(planned)*

```bash
malkuth deploy graphs/research-pipeline.yaml   # validate contracts → start containers
malkuth run research-pipeline --input '{"query": "..."}'
malkuth status                                  # agents healthy?
malkuth run trace <run_id>                      # node timeline
```

Any agent can also be invoked directly, without a graph run:

```bash
malkuth agent invoke researcher --input '{"query": "..."}'   # (planned)
```

## Where to Go Next

- [architecture.md](architecture.md) — how the pieces fit together
- [modules.md](modules.md) — building skillsets, promptsets, memorysets
- [testing.md](testing.md) — writing deterministic tests around LLMs
- [.claude/rules/](../../.claude/rules/README.md) — the full development ruleset
