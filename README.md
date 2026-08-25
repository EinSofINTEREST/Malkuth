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

**Bootstrap phase (pre-v0.1.0).** The ruleset, architecture, and documentation are being
established; framework code has not landed yet. CI quality gates activate automatically
once `pyproject.toml` and `Makefile` appear.

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
