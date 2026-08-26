# Incident Response

**[한국어](../../ko/runbooks/incident-response.md)** | English

How to respond when a Malkuth alert fires. Normative rules:
[05-error-handling.md](../../../.claude/rules/05-error-handling.md).

## Severity

| Level | Meaning | Examples |
|---|---|---|
| **P0** | Whole-system failure | All runs failing, checkpoint loss |
| **P1** | Core capability down | Key agent down, >50% failure rate |
| **P2** | Degraded | Single agent slow, one tool failing |
| **P3** | Minor | Small quality regression |

## First Five Minutes

1. Open the **Overview** dashboard — is this one agent, one graph, or the host?
2. Filter logs by `run_id` — one id links orchestrator → runtime → agentd → protocol.
3. Check the **error code distribution**. The prefix decides the response:
   `LLM_*` (provider), `RT_*` (container), `MCP_*` / `A2A_*` (protocol),
   `GRAPH_*` (topology/state), `STOR_*` (checkpoint).

## By Alert

### AgentHighFailureRate

Task failures exceed 10% for one agent.

1. `malkuth agent logs <agent>` — read the dominant `error_code`.
2. `LLM_001` (rate limit) → see [ModelRateLimited](#modelratelimited).
   `LLM_005` (max turns) → the prompt is likely looping; check the promptset version.
   `MCP_003` → a tool is failing; see the Protocol dashboard.
3. If a recent deploy correlates, roll back the module version — module versioning
   makes this immediate.

### AgentDown

`malkuth_agent_health == 0` for 3 minutes.

1. `malkuth agent inspect <agent>` — compare the manifest against what actually loaded.
2. A failed `initialize()` keeps the container from reaching Ready. The usual causes are
   `MCP_001` (a required MCP server failed to start) and `CFG_002` (a secret key does
   not resolve in any scope).
3. Restart only the affected agent — running graphs resume from their checkpoints.

### ContainerRestartLoop

More than five restarts in ten minutes.

1. Check `reason` on `malkuth_container_restarts_total`. `RT_003` means OOM — raise
   `runtime.resources.memory` or lower concurrency.
2. Repeated `RT_001` usually means the image or entrypoint is wrong; the container never
   gets far enough to report health.
3. After five failures in ten minutes the runtime marks the agent **Failed** and stops
   retrying. Fix the cause, then redeploy.

### ModelRateLimited

The provider is rejecting requests.

1. Reduce the per-agent semaphore so fewer calls are in flight.
2. Switch to a fallback model if the graph tolerates it.
3. `RATE_LIMIT_RETRY` already backs off up to 300s — sustained alerts mean the quota
   itself is too small, not that retries are missing.

### ServiceRunStalled

A service run is active but has made no progress for 30 minutes.

1. Check `malkuth_service_idle_delay_seconds` — a run sitting at the idle ceiling is
   **working as designed** if there is genuinely no input.
2. If input exists, the watcher node is likely failing silently. Read the iteration logs
   (`iteration` field) and confirm the `is_idle` predicate is not always true.

## Escalation

Page for P0/P1. For P2/P3, file an issue with the `run_id` and the error-code
distribution attached — those two make the failure reproducible.

## See Also

- [recovery.md](recovery.md) — restoring runs and reindexing memory
