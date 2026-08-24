# Testing Strategy

**[한국어](../ko/testing.md)** | English

How Malkuth stays deterministic around non-deterministic models. Normative rules:
[06-testing.md](../../.claude/rules/06-testing.md).

## Principles

1. **Tests never call a real LLM** — a scripted `FakeModel` (or recorded cassettes)
   stands in. CI has no provider API keys on purpose; a test that needs one is a bug.
2. **Tests never depend on external services** — Docker fixtures (testcontainers) or
   fakes only.
3. **Deterministic and parallelizable** — time-dependent logic (backoff, health
   intervals, service idle) is tested with injected clocks, never `sleep`.
4. Embeddings use a deterministic hash-based fake — no embedding API calls.

## Test Pyramid & Layout

```
tests/
├── unit/           # no external deps — mirrors src/malkuth structure   (~70%)
├── integration/    # Docker / real MCP server fixtures                  (~25%)
├── e2e/            # full compose stack, fake LLM provider container    (~5%)
└── fixtures/       # FakeModel, fake MCP server, builders, test yamls
```

- Markers: `@pytest.mark.integration`, `@pytest.mark.e2e`
- `make test` runs unit only; `make test-integration` and `make test-e2e` are separate
- E2E runs nightly in CI (`ci-nightly.yml`), not as a PR gate

## What Must Be Covered

| Area | Focus |
|---|---|
| core | manifest / topology / group schema validation, scope resolution |
| orchestrator | config → StateGraph build, routing, state merge, mission & service mode rules |
| protocols | error mapping to `MalkuthError`, A2A allowlist, tool namespacing |
| modules | ref parsing, schema snapshots (skillsets), render goldens (promptsets) |
| memory | space ACL, alias resolution (local > group > global), hybrid search merge, recall budget |
| agentd | tool loop bounds, cancellation cleanup, direct-task handling |
| runtime | container lifecycle, quota rejection (`RT_006`), drain semantics |

Service graphs additionally require: iteration accumulation, idle backoff progression,
drain-mid-iteration resume, and failure-streak halt (`GRAPH_005`) scenarios.

## Quality Gates (CI-enforced)

```bash
make lint          # ruff check + ruff format --check
make typecheck     # mypy — strict on src/malkuth/core
make test          # pytest unit + coverage ≥ 70% (fails below)
make test-integration
```

Coverage targets: 70% minimum overall, 90%+ on critical paths
(`core/`, `orchestrator/`, protocol boundaries), 100% on error-conversion paths.
Check names and merge-gate wiring: [ci/status-checks.md](ci/status-checks.md).
