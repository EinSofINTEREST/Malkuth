# Architecture and Design Principles

## Core Architecture

### System Overview
- **Purpose**: Modular multi-agent orchestration framework
- **Foundation**: LangGraph state graphs over Docker-isolated agents
- **Design Philosophy**: Isolation, composability, explicit contracts

### Architectural Layers

```
┌──────────────────────────────────────────────┐
│  Control Plane Layer                         │
│  (API / CLI / Graph lifecycle management)    │
├──────────────────────────────────────────────┤
│  Orchestration Layer (LangGraph)             │
│  (StateGraph build, config-driven wiring,    │
│   checkpointing, run management)             │
├──────────────────────────────────────────────┤
│  Agent Runtime Layer (Docker)                │
│  (Container lifecycle, Agent Control API,    │
│   resource limits, health monitoring)        │
├──────────────────────────────────────────────┤
│  Protocol Layer — per-agent isolated         │
│  (A2A server/client, MCP client + servers)   │
├──────────────────────────────────────────────┤
│  Module Layer                                │
│  (Skillsets, Promptsets, Graph modules,      │
│   Module registry)                           │
├──────────────────────────────────────────────┤
│  Storage & Observability Layer               │
│  (Checkpoints, Registry DB, Logs, Metrics)   │
└──────────────────────────────────────────────┘
```

### Layer Responsibilities

1. **Control Plane Layer**
   - Graph deployment / start / stop / status API
   - Agent registration and module registry management
   - Run submission and result retrieval

2. **Orchestration Layer**
   - Build LangGraph `StateGraph` from graph topology config (YAML)
   - Route state between agent nodes; evaluate edge conditions
   - Persist state via checkpointer (resume, replay, time-travel)
   - Owns the **graph state**; agents never share state outside it

3. **Agent Runtime Layer**
   - One agent = one Docker container
   - Build, start, health-check, drain, stop containers
   - Expose each agent through a standard **Agent Control API**
   - Enforce resource limits and network policy

4. **Protocol Layer** (per-agent isolated)
   - Each agent owns its A2A server endpoint and its MCP client sessions
   - MCP servers run inside (or as sidecars of) the owning agent's container
   - No protocol resource is shared across agents

5. **Module Layer**
   - Skillsets (tool bundles), promptsets (prompt bundles), graph modules
   - Versioned, independently deployable, swappable at agent restart

6. **Storage & Observability Layer**
   - Graph checkpoints, run history, module registry
   - Structured logs, metrics, traces

## Design Principles

### 1. Isolation
- Each agent MUST run in its own Docker container
- Protocol resources (A2A endpoint, MCP servers) MUST belong to exactly one agent
- Agents MUST NOT share filesystem, process space, or in-memory state
- The only shared data paths are: graph state (via orchestrator) and A2A messages

### 2. Composability (Modular Wiring)
- Agent-to-agent connections are **declared in graph config**, never hardcoded
- Attaching / detaching an agent from a graph MUST be a config change only
- Any agent satisfying the agent contract can be placed at any compatible node

### 3. Explicit Contracts
- Every agent ships a **manifest** declaring model, modules, protocols, resources
- Every graph ships a **topology** declaring nodes, edges, and A2A connection allowlist
- Contracts are validated at deploy time, before any container starts

### 4. Extensibility
- New agents, skillsets, promptsets are added as modules — no core changes
- Protocol adapters (A2A, MCP) are pluggable behind stable interfaces
- Configuration-driven behavior over code-driven behavior

### 5. Reliability
- Agent failure MUST NOT corrupt graph state (checkpoint before/after node execution)
- Unhealthy agents are circuit-broken and restarted by the runtime
- All external calls have timeouts and typed errors

## Current Implementation Status

### 📋 Planned (v0.1.0 — bootstrap)
- **Core Framework**
  - Agent interface + manifest schema (pydantic)
  - Graph topology schema + validation
  - LangGraph orchestrator (config → StateGraph)
- **Agent Runtime**
  - Docker container lifecycle management
  - Agent Control API (invoke / stream / health / card)
  - Agent base image
- **Protocol Layer**
  - Per-agent MCP client + server declaration
  - Per-agent A2A server + connection allowlist
- **Module System**
  - Skillset / promptset loaders + local registry
- **Observability**
  - structlog structured logging
  - Prometheus metrics
- **Quality**
  - pytest infrastructure, testcontainers, CI

### 🔭 Future
- Control plane REST API + Web dashboard
- Remote module registry
- Kubernetes runtime backend (alternative to local Docker)
- Multi-host agent distribution
- Memoryset modules (long-term agent memory)

## Directory Structure

```
malkuth/
├── src/
│   └── malkuth/
│       ├── core/                # 프레임워크 핵심 계약 (모든 레이어가 의존)
│       │   ├── agent.py         # BaseAgent, AgentContext, TaskRequest/Result
│       │   ├── manifest.py      # AgentManifest 스키마 (pydantic)
│       │   ├── errors.py        # MalkuthError + ErrorCategory + 코드 상수
│       │   └── events.py        # TaskEvent, 스트리밍 이벤트 모델
│       │
│       ├── orchestrator/        # LangGraph 오케스트레이션
│       │   ├── builder.py       # Graph config → StateGraph 빌드
│       │   ├── topology.py      # GraphTopology 스키마 + 검증
│       │   ├── state.py         # 공통 state schema 유틸
│       │   └── checkpoint.py    # Checkpointer 설정 (memory/redis/postgres)
│       │
│       ├── runtime/             # Docker 에이전트 런타임
│       │   ├── docker/          # Docker SDK 기반 컨테이너 제어
│       │   ├── control.py       # Agent Control API 클라이언트
│       │   ├── lifecycle.py     # start/drain/stop/restart 정책
│       │   └── health.py        # health check + circuit breaker
│       │
│       ├── protocols/           # 프로토콜 어댑터 (per-agent)
│       │   ├── a2a/             # A2A server/client, AgentCard 생성
│       │   └── mcp/             # MCP client, server launcher, tool 브릿지
│       │
│       ├── modules/             # 모듈 시스템
│       │   ├── skillset.py      # Skillset 스키마 + 로더
│       │   ├── promptset.py     # Promptset 스키마 + 로더 (Jinja2)
│       │   └── registry.py      # 모듈 해석 (ref@version → 경로)
│       │
│       ├── agentd/              # 에이전트 컨테이너 내부 실행 데몬
│       │   ├── server.py        # Agent Control API 서버 (FastAPI)
│       │   ├── executor.py      # 모델 호출 + tool 실행 루프
│       │   └── bootstrap.py     # manifest 로드 → 모듈/프로토콜 초기화
│       │
│       └── cli/                 # malkuth CLI (deploy/run/status/logs)
│
├── agents/                      # 에이전트 정의 (컨테이너 빌드 단위)
│   └── <agent-name>/
│       ├── manifest.yaml        # 에이전트 계약 선언
│       ├── Dockerfile           # 베이스 이미지 확장 (필요 시)
│       └── src/                 # 에이전트 고유 코드 (선택)
│
├── modules/                     # 배포 가능한 모듈 저장소 (로컬 레지스트리)
│   ├── skillsets/<name>/        # skillset.yaml + skills/
│   └── promptsets/<name>/       # promptset.yaml + templates/
│
├── graphs/                      # 그래프 토폴로지 정의
│   └── <graph-name>.yaml
│
├── tests/                       # 모든 테스트 (src 구조 미러링)
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── configs/                     # 환경별 설정 (dev/staging/prod)
├── deployments/
│   └── docker/                  # base image, compose, 네트워크 정의
├── scripts/                     # 개발/운영 스크립트
├── docs/
│   ├── en/                      # English docs (primary)
│   └── ko/                      # Korean translation
├── .claude/
│   └── rules/                   # 본 룰셋
├── pyproject.toml               # 프로젝트 정의 (uv 관리)
├── uv.lock
├── Makefile                     # lint / test / build / up 자동화
└── README.md
```

### Directory Purposes

**`src/malkuth/core/`**: Framework contracts
- Depended on by every other layer; depends on nothing internal
- Pure schemas, interfaces, and errors — no I/O

**`src/malkuth/orchestrator/`**: Graph brain
- The only layer that touches LangGraph APIs directly
- Never talks to Docker or protocols directly — goes through `runtime/`

**`src/malkuth/runtime/`**: Container muscle
- The only layer that touches the Docker SDK
- Exposes agents to the orchestrator as async callables

**`src/malkuth/agentd/`**: In-container daemon
- Ships inside the agent base image
- Loads manifest → initializes promptset/skillset/MCP/A2A → serves Control API

**`agents/`, `modules/`, `graphs/`**: Deployable artifacts
- Pure declaration + assets; framework code lives in `src/` only

**`tests/`**: All test files
- Mirrors the source structure (`tests/unit/orchestrator/`, etc.)
- No test files inside `src/`

## Technology Stack

### Core
- **Language**: Python 3.12+
- **Package Manager**: uv (lockfile 커밋 필수)
- **Orchestration**: LangGraph (latest stable) + langchain-core
- **Schema/Validation**: pydantic v2
- **Async**: asyncio (전 계층 async-first)

### Protocols
- **A2A**: `a2a-sdk` (Agent2Agent protocol)
- **MCP**: `mcp` (official Model Context Protocol Python SDK)

### Runtime
- **Isolation**: Docker Engine 24+, `docker` Python SDK
- **Agent Control API**: FastAPI + uvicorn (컨테이너 내부)
- **Base Image**: `python:3.12-slim` 기반 `malkuth/agent-base`

### Storage
- **Checkpoints**: In-memory (dev) → Redis 7+ / PostgreSQL 15+ (prod)
- **Module Registry**: Local filesystem (v0.1) → PostgreSQL (future)

### Observability
- **Logging**: structlog (JSON output)
- **Metrics**: prometheus-client
- **Tracing**: OpenTelemetry (planned)

### Quality
- **Testing**: pytest + pytest-asyncio + testcontainers
- **Lint/Format**: ruff (lint + format)
- **Type Check**: mypy (strict on `src/malkuth/core/`)

## Control Flow

### Graph Run

```
Client → Control Plane → Orchestrator(StateGraph)
                              │
                              ▼ (node 실행)
                        Runtime Layer ──HTTP──▶ Agent Container
                                                  ├── agentd (Control API)
                                                  ├── promptset render
                                                  ├── model call (LLM)
                                                  ├── skillset tools
                                                  └── MCP servers (stdio/sidecar)
                              │
                              ▼
                        Checkpointer (state 저장)
                              │
                              ▼ (edge 평가)
                        다음 node 또는 END
```

### Stage Definitions

1. **Deploy**: Graph topology + agent manifests 검증 → 컨테이너 기동 → health 확인
2. **Invoke**: 클라이언트가 run 제출 → orchestrator 가 initial state 구성
3. **Node Execution**: orchestrator → runtime → 해당 agent 의 Control API `/invoke`
4. **Tool Loop**: 에이전트 내부에서 모델 ↔ skillset/MCP tool 실행 루프
5. **A2A Call** (선택): 에이전트가 allowlist 내 peer 에이전트를 A2A 로 직접 호출
6. **Checkpoint**: node 완료마다 state 저장 (실패 시 마지막 checkpoint 에서 재개)
7. **Edge Evaluation**: 조건 함수 평가 → 다음 node 라우팅
8. **Complete**: END 도달 → 결과 반환, run 기록 저장

### Two Communication Paths — 반드시 구분

| 경로 | 용도 | 매체 | 규칙 |
|---|---|---|---|
| **Graph state** | 워크플로 단계 간 데이터 전달 | Orchestrator + Checkpointer | 기본 경로. 모든 노드 산출물은 state 로 |
| **A2A direct call** | 실행 중 peer 에이전트에게 위임/질의 | A2A protocol | 그래프 config 의 `connections` allowlist 에 선언된 쌍만 허용 |

Graph state 를 우회하는 사이드채널 (공유 파일, 공유 DB 테이블, 전역 큐) 은 금지.

## Configuration Strategy

### Multi-Environment Support
- Development, Staging, Production configs (`configs/{env}.yaml`)
- Environment variable overrides (`MALKUTH_` prefix)
- Secrets는 환경 변수 주입 — 파일/이미지에 저장 금지

### Framework Configuration

```yaml
# configs/dev.yaml
runtime:
  backend: docker
  network: malkuth-net
  agent_base_image: malkuth/agent-base:0.1.0
  default_resources:
    cpu: "1.0"
    memory: 1Gi
  health_check:
    interval_s: 10
    timeout_s: 3
    unhealthy_threshold: 3

orchestrator:
  checkpointer: memory        # memory | redis | postgres
  max_concurrent_runs: 10
  node_timeout_s: 300

protocols:
  a2a:
    port_range: [9100, 9199]  # 에이전트별 A2A 포트 할당 범위
  mcp:
    startup_timeout_s: 15

registry:
  backend: filesystem
  root: ./modules

observability:
  log_level: DEBUG
  log_format: pretty          # pretty (dev) | json (prod)
  metrics_port: 9090
```

### Contract Validation at Deploy Time

배포 시점에 다음을 검증하고, 하나라도 실패하면 컨테이너를 기동하지 않는다:

1. Graph topology 의 모든 `agent` ref 가 존재하는 manifest 를 가리키는가
2. 모든 manifest 의 skillset/promptset ref 가 registry 에서 해석되는가
3. `connections` 의 caller/callee 가 모두 그래프 노드인가
4. A2A 포트 충돌이 없는가
5. Resource 합계가 호스트 한도 내인가 (경고)

## Scalability Considerations

### Horizontal Scaling

1. **Agent Layer**
   - 동일 manifest 의 에이전트를 N개 replica 로 기동 가능 (stateless 계약 전제)
   - Runtime 이 replica 간 round-robin 라우팅
   - 에이전트 내부 상태 저장 금지 — 상태는 graph state 또는 외부 저장소로

2. **Orchestrator**
   - Run 단위 병렬 실행 (`max_concurrent_runs`)
   - Checkpointer 를 외부화(Redis/Postgres)하면 orchestrator 다중 인스턴스 가능

3. **Runtime Backend**
   - v0.1: 단일 호스트 Docker
   - 향후: runtime 인터페이스 뒤에 Kubernetes backend 교체 (계약 변경 없음)

### Performance Targets (v0.1 기준)

1. **Latency**
   - Agent Control API 오버헤드 < 50ms (모델 호출 제외)
   - Node 간 전이 오버헤드 < 100ms (checkpoint 포함)
   - 컨테이너 cold start < 10s, warm 재시작 < 3s

2. **Availability**
   - 에이전트 crash 시 자동 재시작 (백오프 포함)
   - Run 은 마지막 checkpoint 에서 재개 가능 — 데이터 손실 없음

### Resource Management

1. **Agent Containers**
   - 기본 limit: CPU 1.0 / Memory 1Gi (manifest 로 override)
   - OOM kill 감지 → `RT_003` 에러로 보고 + 재시작 정책 적용

2. **Host**
   - 총 컨테이너 리소스 예약량 모니터링
   - 디스크: 이미지/로그 정리 정책 (dangling image prune)

3. **Model API**
   - Provider rate limit 을 에이전트별 세마포어로 제어
   - 토큰 사용량 metric 수집 (`malkuth_model_tokens_total`)
