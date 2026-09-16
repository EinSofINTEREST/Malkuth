# Access Control Operations

**[한국어](../../ko/runbooks/access-control.md)** | English

Revoking permissions from running agents, and what happens when the access registry is unreachable.
Background: [Access control](../architecture.md#access-control--decided-outside-the-container).

## Emergency Revocation

Something an agent can reach must stop now — a leaked memory space, an outside host, a remote MCP
tool, a connection to another agent.

1. **Revoke the permission.** In the UI: **권한** tab → pick the agent → write the reason → press
   **회수** (or **쓰기 회수** / **전체 회수** for memory) on the declared permission. Through the API:

   ```bash
   curl -X POST -H "Authorization: Bearer $MALKUTH_CONTROL_TOKEN" -H 'content-type: application/json' \
     -d '{"agent": "researcher", "kind": "egress", "target": "api.search.example.com",
          "reason": "incident 311"}' \
     http://127.0.0.1:8700/v1/access/revocations
   ```

   | What to stop | `kind` | `target` | `mode` |
   |---|---|---|---|
   | writing a memory space, keep reading | `memory` | space id, e.g. `group:research:knowledge` | `rw` |
   | any use of a memory space | `memory` | space id | omit |
   | an outside host | `egress` | `host` or `host:port` (`:443` omitted) | omit |
   | one remote MCP tool | `mcp_tool` | `server/tool` | omit |
   | a whole remote MCP server | `egress` | the server's host | omit |
   | calls to another agent | `a2a` | the callee's name | omit |

   A revocation beats declarations and grants, and applies to the agent's **next request**. No
   restart, no redeploy.

2. **Confirm it applies.** The agent's next attempt appears under **최근 거부** with
   `decided_by: operator`, or in `GET /v1/access/agents/{name}` → `denials`. Enforcement points
   log the refusal (`decision: deny`), and `malkuth_access_grants_total{op="revoke"}` counts the
   revocation.

3. **If it must stop everything the agent does,** tear its deployment down
   (`DELETE /v1/deployments/{id}` or **해체**). The agents drain and stop, then the deployment's
   identities are revoked — so even a container that failed to stop is refused by every
   enforcement point. Draining waits for in-flight tasks, so revoke the specific permission first
   (step 1) when seconds matter.

4. **Undo when resolved.** **되돌리기** in the tab, or `DELETE /v1/access/rules/{rule_id}`. The
   record stays with `lifted_at` set.

### What a revocation does not reach

- Data the agent already has: memory it read, responses it received, files it wrote inside its
  container.
- Effects that stay inside the container, including a stdio MCP server's local actions.
- Secrets injected as environment variables. Revoke them by rotating the secret and redeploying.
  The model key and remote MCP credentials are not among them — the egress proxy holds those.
- A widened permission's source. If a permission agent granted it, revoke it here and check
  [AccessGrantRefusalsSpike](incident-response.md#accessgrantrefusalsspike) for the requests that
  led there.

## When the Registry Is Unreachable

Alert: [AccessRegistryUnreachable](incident-response.md#accessregistryunreachable).

While an enforcement point cannot reach the registry:

| Decision | Behaviour |
|---|---|
| not yet cached | **denied** (`MEM_001`, `A2A_004`, `ACC_002` / `503` at the proxy) |
| cached allow from a declaration | keeps working |
| cached allow from a grant or a temporary revocation | keeps working until its `valid_until` |
| a revocation made during the outage | **not applied** until the registry is reachable |

This is deliberate: agents keep doing what they were already allowed to do, and nothing new is
allowed. The cost is that revoking does not work during the outage.

1. Restore the control plane, or the network between it and the `component` named in the alert.
   Enforcement points reconnect on their own. If the registry's version moved while they were cut
   off — a revocation made during the outage — they drop their cached decisions, and the
   revocation applies from the next request.
2. If a revocation cannot wait, stop the affected agents without the control plane:
   `docker stop malkuth-<agent>-<replica>`. When the control plane is back, tear the deployment
   down so its record matches.
3. A registry that answers but **rejects** an enforcement point (a wrong enforcer token) is not an
   outage: that enforcement point drops its cache and denies everything. Fix the token.

## Widening a Permission

Operators do not grant. A worker agent asks the permission agent over A2A, and the registry limits
what it can grant to the expansion ceiling in the group's `spec.access.ceiling` (and `global`'s).
To allow more, change the ceiling in the group declaration — the permission agent cannot change it,
and neither can a request. See [The permission agent](../api.md#the-permission-agent).

## See Also

- [incident-response.md](incident-response.md) — alerts and first response
- [Access registry API](../api.md#access-registry)
