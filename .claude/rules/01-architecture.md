# Architecture and Design Principles

## Core Architecture

### System Overview
- **Purpose**: Modular multi-agent orchestration framework
- **Foundation**: LangGraph state graphs over Docker-isolated agents
- **Composition Model**: 목표(goal) 단위 그래프 = **동등한 에이전트들의 유기적 연결** —
  에이전트 간 우열/계층 없음, 모든 에이전트는 직접 요청 가능한 독립 실행 단위
- **Resource Model**: 에이전트 리소스는 **전역(global) / 소속 그룹(group) / 로컬(local)**
  3계층 스코프로 관리
- **Execution Model**: 달성형(**mission**) run 과 무한 반복형(**service**) run 모두 지원
- **Design Philosophy**: Isolation, composability, explicit contracts — everything is a module

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
│  (Skillsets, Promptsets, Memorysets,         │
│   Graph modules, Module registry)            │
├──────────────────────────────────────────────┤
│  Storage & Observability Layer               │
│  (Checkpoints, Memory store + index,         │
│   Registry DB, Logs, Metrics)                │
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
   - Skillsets (tool bundles), promptsets (prompt bundles), memorysets (memory
     policies), graph modules
   - Versioned, independently deployable, swappable at agent restart

6. **Storage & Observability Layer**
   - Graph checkpoints, run history, module registry
   - Memory Service: context memory spaces + hybrid search index
     ([09-memory-context.md](09-memory-context.md))
   - Structured logs, metrics, traces

## Design Principles

### 1. Isolation
- Each agent MUST run in its own Docker container
- Protocol resources (A2A endpoint, MCP servers) MUST belong to exactly one agent
- Agents MUST NOT share filesystem, process space, or in-memory state
- The only shared data paths are: graph state (via orchestrator), A2A messages,
  and declared scoped memory spaces (group/global)
- Resource access is bounded by scope — **local > group > global** 의 소속 기반 경계

### 2. Composability (Modular Wiring)
- Agent-to-agent connections are **declared in graph config**, never hardcoded
- Attaching / detaching an agent from a graph MUST be a config change only
- Any agent satisfying the agent contract can be placed at any compatible node
- 에이전트 간 관계는 전부 **대등한 peer 연결** — 우열/소속 선언이 존재하지 않는다

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

## Interaction Model — 동등한 에이전트, 세 가지 접근

그래프는 **goal(목표) 단위**로 구성된다. 목표를 달성하기 위해 여러 에이전트가 노드로
배치되고 유기적으로 연결되지만, **에이전트 간 우열/계층 관계는 존재하지 않는다** —
모든 에이전트는 동등한 peer 이며, 각각 직접 요청을 받을 수 있는 독립 실행 단위다.

```
        Graph = goal 단위 ("research-pipeline", "feed-monitor", ...)

   START ──▶ [ planner ] ──▶ [ researcher ] ──▶ [ writer ] ──▶ END / loop
                  ▲                │  ▲
                  └── A2A peer ────┘  │ (connections 선언 기반, 방향 자유)
                                      │
   Client ────────── direct request ──┘ (그래프와 무관하게 어느 에이전트든 직접 호출)
```

### Interaction Rules

1. **Orchestrated run**: 그래프가 edges/조건에 따라 에이전트를 호출 (mission / service).
   순서는 배선의 결과일 뿐 — 앞 노드가 뒤 노드의 상위가 아니다
2. **Direct request**: 클라이언트는 그래프 run 과 무관하게 **어느 에이전트에게든
   인터랙티브하게 직접 요청**할 수 있다 (단독 태스크 / 스트리밍 대화).
   Control Plane → Runtime → 해당 에이전트의 Control API 경로 사용
3. **Peer call**: 실행 중 에이전트는 allowlist 에 선언된 peer 에게 A2A 로 위임/질의한다.
   호출자와 피호출자는 대등하다 — 어느 방향이든 선언만 하면 허용되며,
   상호 선언 시 양방향 협업도 가능 (깊이 상한으로 폭주만 방지)
4. **오케스트레이터는 라우터일 뿐**: 라우팅과 state 관리를 담당하는 인프라이지
   에이전트 위에 군림하는 "슈퍼 에이전트"가 아니다 — 협업 구조는 전적으로
   배선(edges/connections)이 정의한다
5. **격리 유지**: 모든 에이전트는 독립 컨테이너 — 프로세스/파일시스템 공유 금지

배선 스펙은 [04-module-system.md](04-module-system.md), 직접 요청 계약은
[02-agent-implementation.md](02-agent-implementation.md) 참조.

## Execution Modes — Mission and Service

목표의 성격에 따라 그래프는 두 가지 모드로 실행된다. 모드는 그래프 config 에 선언한다.

| | **Mission (달성형)** | **Service (상주형)** |
|---|---|---|
| 목표 | 특정 기능/산출물 완성을 위한 오케스트레이션 | 무한히 반복해야 할 과업 |
| 종료 | END 도달 시 완료, 결과 반환 | 자연 종료 없음 — 운영자 stop / 명시적 stop 조건 |
| 토폴로지 | END 필수, cycle 은 `max_iterations` 필수 | 무한 cycle 허용, idle 정책 필수 |
| Checkpoint | node 단위 | node + iteration 단위 (재시작 시 이어서) |
| 예시 | 리서치 보고서 생성, 기능 구현 파이프라인 | 피드 감시, 큐 소비, 주기 리포팅 |

### Mode Rules

1. **Mission**: `START → ... → END`. 완료 시 최종 state 반환, run 종료
2. **Service**: iteration loop 로 영구 실행
   - Iteration 마다 checkpoint — 프로세스/호스트 재시작 시 마지막 iteration 에서 재개
   - **Idle 정책 필수**: 처리할 작업이 없으면 exponential backoff —
     busy-loop 로 모델 호출을 낭비하는 것 금지
   - 중단은 drain 방식 — 진행 중 iteration 완료 후 정지
   - 연속 실패 임계(`max_failure_streak`) 초과 시 run 정지 + 알림 (crash loop 방지)
3. **Recurring mission** (스케줄 반복): iteration 간 state 연속성이 **불필요**하면 service
   대신 control plane 스케줄러로 mission run 을 반복 실행한다.
   연속성 필요(누적 상태, 중복 방지 등) → **service** / 매회 독립 → **recurring mission**

## Resource Scoping — Global / Group / Local

에이전트를 위한 리소스(secrets, 메모리 space, artifact 저장, 리소스 quota)는
**전역(global) / 소속 그룹(group) / 로컬(local)** 3계층 스코프로 관리한다.

```
global  ─ 모든 에이전트                (groups/global.yaml — 예약 그룹)
group   ─ 소속 그룹 멤버               (groups/<name>.yaml)
local   ─ 개별 에이전트                (agents/<name>/manifest.yaml)
```

### Group Concept

1. **그룹 = 리소스 경계, 우열 아님**: 그룹은 에이전트의 소속 단위이자 리소스 관리
   경계일 뿐이다 — 그룹 간에도, 그룹 내에서도 에이전트 우열 관계를 만들지 않는다
   (peer 원칙 유지)
2. **소속**: 에이전트는 manifest `metadata.group` 으로 **최대 하나의 그룹**에 소속.
   미선언 시 global 에만 속한다. 모든 에이전트는 암묵적으로 예약 그룹 `global` 의 멤버
3. **그룹은 연결이 아니다**: 같은 그룹이라도 A2A 자동 허용 없음 — 연결은 여전히
   그래프 `connections` 선언만 ([03-protocol-integration.md](03-protocol-integration.md)).
   그래프는 서로 다른 그룹의 에이전트를 자유롭게 배선한다 (cross-group wiring 허용)
4. **해석 순서**: 동일 키/이름의 리소스는 **local > group > global** —
   가까운 스코프가 우선 (shadowing 허용)

### Scoped Resources

| 리소스 | global | group | local |
|---|---|---|---|
| Secrets (env) | 전사 공용 키 | 그룹 공용 키 (멤버만) | 에이전트 전용 키 |
| Memory space | 전역 지식 (기본 ro) | 그룹 지식 베이스 (멤버 rw 기본) | 에이전트 장기 기억 |
| Artifact 저장 | 전역 공유 산출물 | 그룹 산출물 | 에이전트 산출물 |
| Resource quota | 호스트 총량 | 그룹 합계 상한 | manifest limit |

상세 규칙: secrets 는 [02-agent-implementation.md](02-agent-implementation.md),
memory 는 [09-memory-context.md](09-memory-context.md).

### Group Specification

```yaml
# groups/research.yaml
apiVersion: malkuth/v1
kind: Group
metadata:
  name: research
  description: 리서치 도메인 에이전트 그룹

spec:
  quotas:                        # 소속 에이전트 리소스 합계 상한 (배포/기동 시 검증)
    cpu: "8.0"
    memory: 16Gi
    max_agents: 10

  secrets:                       # 그룹 스코프 secret 키 — 멤버 에이전트만 주입 가능
    - SEARCH_API_KEY

  memory:                        # 그룹 스코프 memory space — 09 참조
    spaces:
      - ref: memorysets/domain-knowledge@0.1.0
        as: knowledge
        mode: rw                 # 멤버 기본 권한 (rw | ro)

  artifacts:
    quota: 50Gi
```

### Group Rules

1. 예약 그룹 `global` (`groups/global.yaml`) 은 전역 스코프 리소스 선언 전용 —
   에이전트가 `metadata.group: global` 로 직접 소속을 선언하는 것은 금지 (검증 차단)
2. 그룹 이동 = manifest 변경 (version bump + 재배포) — 이전 그룹 리소스 접근은
   즉시 상실 (토큰 재발급), local 리소스는 유지
3. Quota 는 배포 검증 + 기동 시 재검증 — 그룹 합계 초과 시 기동 거부 (`RT_006`)
4. 그룹 정의 삭제는 소속 에이전트가 0이 될 때만 허용

## Current Implementation Status

### 📋 Planned (v0.1.0 — bootstrap)
- **Core Framework**
  - Agent interface + manifest schema (pydantic)
  - Graph topology schema + validation (mission/service 모드 포함)
  - Group schema + 리소스 스코프 해석 (global/group/local)
  - LangGraph orchestrator (config → StateGraph)
  - Service run loop (iteration checkpoint + idle backoff)
- **Agent Runtime**
  - Docker container lifecycle management
  - Agent Control API (invoke / stream / health / card)
  - Agent base image
- **Protocol Layer**
  - Per-agent MCP client + server declaration
  - Per-agent A2A server + connection allowlist
- **Module System**
  - Skillset / promptset / memoryset loaders + local registry
- **Memory System**
  - Memory Service (space + access token) / 하이브리드 인덱스 (vector + lexical)
  - Auto-recall + `memory_search` tool
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
- 전용 vector DB backend (Qdrant 등 — MemoryStore 구현 추가)

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
│       │   ├── memoryset.py     # Memoryset 스키마 (정책 선언)
│       │   └── registry.py      # 모듈 해석 (ref@version → 경로)
│       │
│       ├── memory/              # Memory Service (컨텍스트 메모리 + 인덱스)
│       │   ├── service.py       # space 관리 + access 토큰 검증 API
│       │   ├── store.py         # MemoryStore 추상 + sqlite/postgres 구현
│       │   ├── index.py         # 하이브리드 인덱스 (vector + lexical + filter)
│       │   └── recall.py        # 검색/병합(RRF) + 컨텍스트 주입 예산
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
│   ├── promptsets/<name>/       # promptset.yaml + templates/
│   └── memorysets/<name>/       # memoryset.yaml (메모리 정책)
│
├── graphs/                      # 그래프 토폴로지 정의
│   └── <graph-name>.yaml
│
├── groups/                      # 그룹 정의 (리소스 스코프 경계)
│   ├── global.yaml              # 예약 그룹 — 전역 스코프 리소스 선언
│   └── <group-name>.yaml
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
- **Memory & Index**: SQLite + sqlite-vec/FTS5 (dev) → PostgreSQL + pgvector (prod)
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
5. **Peer Call** (선택): 에이전트가 allowlist 내 peer 에이전트를 A2A 로 직접 호출
   (위임/질의 — 어느 쪽도 상위가 아님)
6. **Checkpoint**: node 완료마다 state 저장 (실패 시 마지막 checkpoint 에서 재개)
7. **Edge Evaluation**: 조건 함수 평가 → 다음 node 라우팅
8. **Complete / Iterate**: mission 은 END 도달 → 결과 반환, run 기록 저장.
   service 는 iteration checkpoint 후 다음 iteration 으로 순환 (idle 시 backoff)

### Communication & Access Paths — 반드시 구분

| 경로 | 용도 | 매체 | 규칙 |
|---|---|---|---|
| **Graph state** | 워크플로 단계 간 데이터 전달 | Orchestrator + Checkpointer | 기본 경로. 모든 노드 산출물은 state 로 |
| **A2A peer call** | 실행 중 peer 에이전트에게 위임/질의 (대등) | A2A protocol | 그래프 config 의 `connections` allowlist 에 선언된 방향만 허용 |
| **Direct request** | 클라이언트 → 특정 에이전트 직접 요청 (인터랙티브 포함) | Control Plane → Agent Control API | 그래프 run 과 독립된 단독 태스크 — graph state 를 건드리지 않음 |
| **Scoped memory** | group/global space 를 통한 지식 축적/공유 | Memory Service | memoryset 선언 + 소속 기반 접근 ([09-memory-context.md](09-memory-context.md)) |

Graph state 를 우회하는 사이드채널 (공유 파일, 공유 DB 테이블, 전역 큐) 은 금지.
유일한 예외는 **선언된 group/global memory space** — 소속과 스코프로 접근이 검증되는
공유 경로다.

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
  max_concurrent_runs: 10     # mission run 동시 실행 상한
  max_service_runs: 5         # 상주(service) run 상한 — 장기 점유 슬롯 분리 관리
  node_timeout_s: 300
  service_defaults:
    idle_min_delay_s: 30
    idle_max_delay_s: 600
    max_failure_streak: 5

protocols:
  a2a:
    port_range: [9100, 9199]  # 에이전트별 A2A 포트 할당 범위
  mcp:
    startup_timeout_s: 15

registry:
  backend: filesystem
  root: ./modules

memory:
  backend: sqlite               # sqlite (dev) | postgres (prod)
  index_lag_target_s: 5         # 비동기 인덱싱 목표 지연
  run_scope_retention_days: 30  # run scope 보존 (checkpoint 와 동일)

observability:
  log_level: DEBUG
  log_format: pretty          # pretty (dev) | json (prod)
  metrics_port: 9090
```

### Contract Validation at Deploy Time

배포 시점에 다음을 검증하고, 하나라도 실패하면 컨테이너를 기동하지 않는다:

1. Graph topology 의 모든 `agent` ref 가 존재하는 manifest 를 가리키는가
2. 모든 manifest 의 skillset/promptset/memoryset ref 가 registry 에서 해석되는가
3. 모든 manifest 의 `group` 이 존재하는 그룹을 가리키는가 (`global` 직접 소속 금지)
4. `env_allowlist` 의 각 키가 스코프 체인 (local > group > global) 에서 해석되는가
5. `connections` 의 caller/callee 가 모두 그래프 노드인가
6. Mode 별 토폴로지 규칙 충족 (mission: END 도달 / service: idle 정책 선언)
7. A2A 포트 충돌이 없는가
8. 그룹별 리소스 합계가 quota 이내인가, 전체 합계가 호스트 한도 내인가 (초과 시 경고/거부)

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
