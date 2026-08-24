# Malkuth Development Rules

This directory contains the comprehensive ruleset for developing **Malkuth**, a modular
multi-agent orchestration framework built on LangGraph. These rules guide the implementation
of an extensible agent runtime with per-agent isolation and freely composable agent graphs.

## Overview

**Malkuth** is a framework designed to:
- Orchestrate multiple AI agents through LangGraph state graphs
- Compose each goal as a graph of **equal, directly addressable agents** — no hierarchy
  between agents; every agent also accepts interactive direct requests
- Run goal-oriented **mission** graphs to completion, and **service** graphs that repeat
  indefinitely for perpetual tasks
- Isolate each agent in its own Docker container, controlled through a standard runtime API
- Support A2A (Agent2Agent) and MCP (Model Context Protocol) **per agent, in isolation**
- Connect and disconnect agents freely through config-driven, module-style graph wiring
- Provide skillsets and promptsets as independent, versioned, swappable modules —
  a solution is assembled from modules, not written from scratch

## Ruleset Structure

The rules are organized into specialized domains:

### [01-architecture.md](01-architecture.md)
**Core System Design and Architecture**

Covers:
- Overall system architecture and layers
- Technology stack requirements
- Directory structure and organization
- Control flow: graph invocation → agent runtime → protocol layer
- Scalability and isolation boundaries
- Multi-environment configuration

Read this first to understand:
- How components fit together
- Design principles and patterns
- Infrastructure requirements
- Where each concern lives

### [02-agent-implementation.md](02-agent-implementation.md)
**Agent Development Standards**

Covers:
- Core agent interface design
- Agent manifest specification
- Docker isolation rules (image, resources, network, secrets)
- Agent lifecycle and the Agent Control API
- Health checks and graceful shutdown

Essential for:
- Implementing new agents
- Packaging agents as containers
- Controlling agents through the runtime layer

### [03-protocol-integration.md](03-protocol-integration.md)
**A2A and MCP Integration Rules**

Covers:
- Per-agent protocol isolation principles
- A2A server/client rules, AgentCard, task lifecycle
- Inter-agent call authorization (connection allowlist)
- MCP server declaration, transports, tool namespacing
- Protocol error mapping and version pinning

Key for:
- Wiring agents to talk to each other over A2A
- Attaching MCP tool servers to individual agents
- Keeping protocol resources from leaking across agent boundaries

### [04-module-system.md](04-module-system.md)
**Skillsets, Promptsets, and Graph Modules**

Covers:
- Module types and directory specifications
- Skillset interface and loading isolation
- Promptset templates, variables, locale support
- Graph topology modules (nodes, edges, connections)
- Registry, versioning, and compatibility rules

Key for:
- Building reusable skill and prompt modules
- Attaching/detaching agents from graphs without code changes
- Managing module versions and compatibility

### [05-error-handling.md](05-error-handling.md)
**Error Handling, Monitoring, and Observability**

Covers:
- Error taxonomy and the `MalkuthError` type
- Layer rules — where typed errors are required
- Retry policies and circuit breakers
- Structured logging standards (structlog)
- Metrics collection with Prometheus
- Health checks, alerting, incident response

Critical for:
- Production reliability
- Debugging multi-agent runs
- Operational excellence

### [06-testing.md](06-testing.md)
**Testing Strategy and Quality Assurance**

Covers:
- Unit, integration, and E2E testing with pytest
- Mocking LLMs, MCP servers, and A2A peers
- Container-based integration tests (testcontainers)
- Graph-level tests with in-memory checkpointers
- Coverage requirements, linting, CI workflows

Important for:
- Ensuring deterministic tests around non-deterministic models
- Preventing regressions in agent wiring
- Maintaining test coverage

### [07-code-style.md](07-code-style.md)
**Code Style and Conventions**

Covers:
- Python formatting standards (ruff, 4-space indentation, line length 100)
- Naming conventions and type hint requirements
- Async patterns and pydantic model design
- Comments and documentation language policy (English + Korean)
- YAML configuration style
- Git commit / branch / PR conventions

Essential for:
- Consistent codebase
- Code readability
- Team collaboration

### [08-workflow.md](08-workflow.md)
**AI Workflow Conventions**

Covers:
- Autonomous progression policy — when AI proceeds without user approval
- Exception zones (system changes / destructive perms / external impact / ambiguous scope)
- Issue-first policy, commit-per-TODO policy
- PR auto-creation policy with template + closing reference
- Label / Issue Type metadata policy

Essential for:
- AI-assisted development efficiency
- Safe boundary enforcement on destructive / external operations

## Quick Start Guide

### For New Developers

1. **Start Here**: Read [01-architecture.md](01-architecture.md) for system overview
2. **Code Standards**: Read [07-code-style.md](07-code-style.md) for style guidelines
3. **Set Up Environment**: Follow technology stack requirements (Python 3.12+, uv, Docker)
4. **Understand Control Flow**: Review how a graph run reaches an agent container
5. **Reference Rules**: Use relevant sections while coding

### For Specific Tasks

**Adding a New Agent:**
1. Review [02-agent-implementation.md](02-agent-implementation.md) — agent interface + manifest
2. Declare protocols per [03-protocol-integration.md](03-protocol-integration.md)
3. Reference skillsets/promptsets per [04-module-system.md](04-module-system.md)
4. Add tests per [06-testing.md](06-testing.md)

**Building a New Skillset or Promptset:**
1. Review [04-module-system.md](04-module-system.md) — module specification
2. Follow versioning and compatibility rules
3. Add module-level tests

**Wiring a New Graph:**
1. Choose the execution mode — mission (terminating) vs service (perpetual),
   per [01-architecture.md](01-architecture.md)
2. Review [04-module-system.md](04-module-system.md) — graph topology modules
3. Validate topology (no dangling refs) before deployment
4. Add graph-level integration tests per [06-testing.md](06-testing.md)

**Debugging Production Issues:**
1. Check [05-error-handling.md](05-error-handling.md) — incident response
2. Trace by `run_id` through orchestrator and agent logs
3. Follow debugging procedures, update runbooks if needed

## Key Principles

### 1. Isolation First
- One agent = one Docker container
- Protocol resources (A2A endpoints, MCP servers) belong to exactly one agent
- No shared mutable state between agents outside the graph state

### 2. Composability
- Agents connect only through declared graph edges and A2A connections
- All agents are equal peers — no rank or ownership between agents, in any direction
- Attaching/detaching an agent is a config change, not a code change
- Skillsets and promptsets are swappable without touching agent code

### 3. Explicit Contracts
- Every agent declares its manifest (model, modules, protocols, resources)
- Every graph declares its topology and connection allowlist
- Version everything: agents, skillsets, promptsets, graphs

### 4. Reliability
- Handle failures gracefully; agent failure must not corrupt graph state
- Retry with backoff, circuit-break unhealthy agents
- Monitor and alert proactively

### 5. Maintainability
- Write clear, documented code
- Follow Python best practices
- Comprehensive testing
- Keep dependencies minimal

## Development Workflow

### Before Writing Code

1. **Understand the Requirement**
   - What problem are we solving?
   - Which layer does this affect (orchestration / runtime / protocol / module)?
   - Are there existing patterns to follow?

2. **Review Relevant Rules**
   - Check the appropriate ruleset section
   - Understand interfaces and patterns
   - Review error handling requirements
   - Plan test coverage

3. **Design Before Implementation**
   - Sketch the control flow
   - Identify isolation boundaries being crossed
   - Plan for failure cases
   - Consider performance impact

### While Writing Code

1. **Follow the Style Guide** ([07-code-style.md](07-code-style.md))
2. **Follow the Architecture Rules** — use prescribed interfaces and layer boundaries
3. **Write Tests Alongside** — unit tests for logic, integration tests for wiring
4. **Keep it Simple** — no premature abstraction, no dead code

### After Writing Code

1. **Self-Review** (Use [07-code-style.md](07-code-style.md) checklist)
2. **Quality Checks** — ruff, mypy, pytest, coverage ≥ 70%
3. **Integration Verification** — run the affected graph in the dev environment,
   check metrics and logs, document new configurations

## Common Patterns

### Agent Implementation

```python
# Follow the agent interface from 02-agent-implementation.md
class ResearchAgent(BaseAgent):
    async def invoke(self, task: TaskRequest) -> TaskResult:
        prompt = self.promptset.render("research", query=task.input)
        tools = self.skillset.tools() + self.mcp.tools()
        result = await self.model.run(prompt, tools=tools)
        return TaskResult(output=result.content)
```

### Graph Wiring (config, not code)

```yaml
# graphs/research-pipeline.yaml — follow 04-module-system.md
spec:
  mode: mission                 # mission(달성형) | service(상주형)
  nodes:
    - id: planner
      agent: agents/planner@0.1.0
    - id: researcher
      agent: agents/researcher@0.1.0
  edges:
    - {from: START, to: planner}
    - {from: planner, to: researcher, condition: needs_research}
    - {from: researcher, to: END}
```

### Error Handling

```python
# Follow error patterns from 05-error-handling.md
try:
    result = await client.call_tool(name, args)
except McpTransportError as err:
    raise MalkuthError(
        category=ErrorCategory.MCP,
        code="MCP_004",
        message="mcp transport disconnected",
        agent=self.name,
        retryable=True,
    ) from err
```

## Updates and Evolution

These rules are living documents and should evolve with the project:

### When to Update Rules

- New patterns emerge across the codebase
- Best practices are discovered
- Production issues reveal gaps
- New protocol versions (A2A / MCP) are adopted

### How to Update

1. Propose changes via discussion
2. Update relevant ruleset file
3. Update this README if structure changes
4. Communicate changes to team
5. Update code to follow new rules

## Additional Resources

### External Documentation

- LangGraph: https://langchain-ai.github.io/langgraph/
- A2A Protocol: https://a2a-protocol.org/
- Model Context Protocol: https://modelcontextprotocol.io/
- PEP 8 Style Guide: https://peps.python.org/pep-0008/

### Internal Documentation

- Architecture Docs: `docs/architecture/`
- API Documentation: `docs/api/`
- Runbooks: `docs/runbooks/`

## Getting Help

- Review the relevant ruleset section first
- Check existing code for examples
- Ask in team chat with specific questions
- Reference the ruleset section in your question

---

**Remember**: These rules exist to ensure consistency, quality, and maintainability. When in
doubt, follow the rules. If the rules don't cover a scenario, that's an opportunity to improve
them.
