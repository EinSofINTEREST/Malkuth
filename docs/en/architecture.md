# Architecture Overview

**[한국어](../ko/architecture.md)** | English

Malkuth orchestrates multiple AI agents through LangGraph state graphs, with each agent
isolated in its own Docker container. This document is a condensed overview; the
normative rules are in [01-architecture.md](../../.claude/rules/01-architecture.md).

## Layers

```
┌──────────────────────────────────────────────┐
│  Control Plane Layer                         │  API / CLI / graph lifecycle
├──────────────────────────────────────────────┤
│  Orchestration Layer (LangGraph)             │  StateGraph build, routing, checkpoints
├──────────────────────────────────────────────┤
│  Agent Runtime Layer (Docker)                │  container lifecycle, Agent Control API
├──────────────────────────────────────────────┤
│  Protocol Layer — per-agent isolated         │  A2A server/client, MCP client + servers
├──────────────────────────────────────────────┤
│  Module Layer                                │  skillsets, promptsets, memorysets, graphs
├──────────────────────────────────────────────┤
│  Storage & Observability Layer               │  checkpoints, memory + index, logs, metrics
└──────────────────────────────────────────────┘
```

## Interaction Model — Equal Agents

Agents are **equal peers**: there is no hierarchy, and every agent is directly
addressable. Three access paths exist:

1. **Orchestrated run** — the graph invokes agents by its edges and conditions.
   Order comes from wiring, not rank.
2. **Direct request** — a client can interactively invoke any agent, independent of any
   graph run (`node_id=None`, `default` prompt template, no graph-state access).
3. **Peer call (A2A)** — an agent delegates to or queries another agent, allowed only for
   pairs declared in the graph's `connections` allowlist. Symmetric: either direction can
   be declared; mutual declaration enables two-way collaboration.

The orchestrator is a router, not a "super agent" — collaboration structure is defined
entirely by wiring.

## Execution Modes

| | **Mission** | **Service** |
|---|---|---|
| Goal | Complete a specific outcome | A perpetually repeating task |
| Termination | Reaches END, returns result | Operator stop / explicit stop condition |
| Topology | END required; cycles need `max_iterations` | Infinite cycles allowed; idle policy required |
| Checkpoint | Per node | Per node + per iteration (resumes across restarts) |

Service graphs back off exponentially when idle (no busy-looping model calls) and halt
with an alert after a configured failure streak. If iterations don't need shared state,
prefer a scheduled (recurring) mission instead.

## Resource Scoping — Global / Group / Local

Agent resources (secrets, memory spaces, artifacts, quotas) are managed at three scopes:

```
global  ─ every agent              (groups/global.yaml — reserved group)
group   ─ members of the group     (groups/<name>.yaml)
local   ─ a single agent           (agents/<name>/manifest.yaml)
```

- A group is a **resource boundary, not a rank** — it grants no calling privileges and
  creates no hierarchy. Wiring (edges/connections) and scoping are orthogonal axes.
- An agent belongs to at most one group (`metadata.group`); everyone is implicitly a
  member of the reserved `global` group.
- Name collisions resolve nearest-first: **local > group > global**.

## Context Memory

Agents accumulate searchable context in declared memory spaces
(scopes: `run` / `local` / `group` / `global`), served by the framework Memory Service:

- Hybrid index per space: vector (embeddings) + lexical (BM25) + metadata filters,
  merged with reciprocal rank fusion
- Token-budgeted auto-recall at task start; further searches via an explicit tool
- Append-only entries with provenance; retention and compaction declared per memoryset

See [09-memory-context.md](../../.claude/rules/09-memory-context.md).

## Control Flow (Graph Run)

```
Client → Control Plane → Orchestrator (StateGraph)
                             │ node execution
                             ▼
                       Runtime Layer ──HTTP──▶ Agent Container (agentd)
                             │                   ├─ promptset render
                             ▼                   ├─ model call + tool loop
                       Checkpointer              ├─ skillset / MCP tools
                             │                   └─ A2A peer calls (allowlisted)
                             ▼
                       edge evaluation → next node / END / next iteration
```

## Technology Stack

| Concern | Choice |
|---|---|
| Language / packaging | Python 3.12+, uv |
| Orchestration | LangGraph + langchain-core |
| Protocols | `a2a-sdk`, `mcp` (official SDKs, lockfile-pinned) |
| Runtime | Docker Engine 24+, `docker` SDK; FastAPI in-container |
| Checkpoints | in-memory (dev) → Redis / PostgreSQL (prod) |
| Memory & index | SQLite + sqlite-vec/FTS5 (dev) → PostgreSQL + pgvector (prod) |
| Observability | structlog, prometheus-client |
| Quality | ruff, mypy, pytest (+ testcontainers) |
