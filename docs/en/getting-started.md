# Getting Started

**[한국어](../ko/getting-started.md)** | English

> **Status**: The framework layers are implemented and the reference solution validates
> end to end. Commands marked *(needs a running stack)* require deployed containers,
> which is the remaining integration work.

## Prerequisites

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) (package manager — lockfile is committed)
- Docker Engine **24+** (agent isolation runtime)
- `make` (task automation)

## Setup

```bash
git clone https://github.com/EinSofINTEREST/Malkuth.git
cd Malkuth
uv sync                 # install pinned dependencies
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

### 4. Validate before deploying

Contract validation runs before anything starts — **if any of the eight checks fails,
no container is started**:

```bash
malkuth validate                                # every graph in the repository
malkuth deploy graphs/research-pipeline.yaml    # one graph
malkuth status                                  # what is declared here
malkuth config dev                              # resolved settings for an environment
```

Both `validate` and `deploy` exit non-zero when a check fails, so they compose with
scripts. Add `--json` for machine-readable output.

Integrity checks compare records against reality — orphaned checkpoints, dangling
module refs, ghost containers:

```bash
malkuth check observed-state.yaml
```

### 5. Run a graph *(needs a running stack)*

```bash
malkuth run research-pipeline --input '{"query": "..."}'
malkuth run trace <run_id>                      # node timeline
malkuth agent invoke researcher --input '{"query": "..."}'   # direct request
```

## Where to Go Next

- [architecture.md](architecture.md) — how the pieces fit together
- [modules.md](modules.md) — building skillsets, promptsets, memorysets
- [testing.md](testing.md) — writing deterministic tests around LLMs
- [.claude/rules/](../../.claude/rules/README.md) — the full development ruleset
