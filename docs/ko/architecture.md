# 아키텍처 개요

한국어 | **[English](../en/architecture.md)**

Malkuth 는 LangGraph state graph 로 여러 AI 에이전트를 오케스트레이션하며, 각 에이전트를
독립 Docker 컨테이너로 격리합니다. 본 문서는 요약본이며, 규범 규칙은
[01-architecture.md](../../.claude/rules/01-architecture.md) 에 있습니다.

## 계층 구조

```
┌──────────────────────────────────────────────┐
│  Control Plane Layer                         │  API / CLI / 그래프 lifecycle
├──────────────────────────────────────────────┤
│  Orchestration Layer (LangGraph)             │  StateGraph 빌드, 라우팅, checkpoint
├──────────────────────────────────────────────┤
│  Agent Runtime Layer (Docker)                │  컨테이너 lifecycle, Agent Control API
├──────────────────────────────────────────────┤
│  Protocol Layer — 에이전트별 격리            │  A2A server/client, MCP client + servers
├──────────────────────────────────────────────┤
│  Module Layer                                │  스킬셋, 프롬프트셋, 메모리셋, 그래프
├──────────────────────────────────────────────┤
│  Storage & Observability Layer               │  checkpoint, 메모리 + 인덱스, 로그, 메트릭
└──────────────────────────────────────────────┘
```

## 상호작용 모델 — 동등한 에이전트

에이전트는 **동등한 peer** 입니다. 우열/계층이 없고, 모든 에이전트는 직접 요청 가능합니다.
세 가지 접근 경로:

1. **Orchestrated run** — 그래프가 edges/조건에 따라 에이전트를 호출.
   순서는 배선의 결과일 뿐 서열이 아닙니다.
2. **Direct request** — 클라이언트가 그래프 run 과 무관하게 어느 에이전트든 인터랙티브하게
   직접 호출 (`node_id=None`, `default` 프롬프트 템플릿, graph state 불간섭).
3. **Peer call (A2A)** — 그래프 `connections` allowlist 에 선언된 쌍만 위임/질의 가능.
   대칭적 — 어느 방향이든 선언하면 허용, 상호 선언 시 양방향 협업.

오케스트레이터는 라우터일 뿐 "슈퍼 에이전트"가 아닙니다 — 협업 구조는 전적으로 배선이
정의합니다.

## 실행 모드

| | **Mission (달성형)** | **Service (상주형)** |
|---|---|---|
| 목표 | 특정 산출물/기능 완성 | 무한히 반복되는 과업 |
| 종료 | END 도달 시 결과 반환 | 운영자 stop / 명시적 stop 조건 |
| 토폴로지 | END 필수, cycle 은 `max_iterations` 필수 | 무한 cycle 허용, idle 정책 필수 |
| Checkpoint | node 단위 | node + iteration 단위 (재시작 시 이어서) |

Service 그래프는 유휴 시 exponential backoff 로 대기하고 (모델 호출 busy-loop 금지),
연속 실패 임계 초과 시 정지 + 알림됩니다. Iteration 간 state 공유가 불필요하면 스케줄
반복 mission 을 사용합니다.

## 리소스 스코프 — Global / Group / Local

에이전트 리소스(secrets, 메모리 space, artifact, quota)는 3계층 스코프로 관리합니다:

```
global  ─ 모든 에이전트            (groups/global.yaml — 예약 그룹)
group   ─ 소속 그룹 멤버           (groups/<name>.yaml)
local   ─ 개별 에이전트            (agents/<name>/manifest.yaml)
```

- 그룹은 **리소스 경계이지 서열이 아닙니다** — 호출 권한을 부여하지 않고 계층도 만들지
  않습니다. 배선(edges/connections)과 스코프는 직교하는 두 축입니다.
- 에이전트는 최대 하나의 그룹에 소속 (`metadata.group`), 모두가 예약 그룹 `global` 의
  암묵적 멤버입니다.
- 이름 충돌은 **local > group > global** 순으로 가까운 스코프가 우선합니다.

## 컨텍스트 메모리

에이전트는 선언된 memory space (스코프: `run` / `local` / `group` / `global`) 에 검색
가능한 컨텍스트를 축적합니다:

- Space 별 하이브리드 인덱스: vector(임베딩) + lexical(BM25) + 메타데이터 필터, RRF 병합
- 태스크 진입 시 토큰 예산 기반 auto-recall, 이후는 명시적 tool 로 검색
- Append-only + 출처(provenance) 필수, memoryset 별 보존/compaction 선언

상세: [09-memory-context.md](../../.claude/rules/09-memory-context.md).

## 제어 흐름 (Graph Run)

```
Client → Control Plane → Orchestrator (StateGraph)
                             │ node 실행
                             ▼
                       Runtime Layer ──HTTP──▶ Agent Container (agentd)
                             │                   ├─ promptset 렌더
                             ▼                   ├─ 모델 호출 + tool loop
                       Checkpointer              ├─ skillset / MCP tools
                             │                   └─ A2A peer 호출 (allowlist)
                             ▼
                       edge 평가 → 다음 node / END / 다음 iteration
```

## 기술 스택

| 영역 | 선택 |
|---|---|
| 언어 / 패키징 | Python 3.12+, uv |
| 오케스트레이션 | LangGraph + langchain-core |
| 프로토콜 | `a2a-sdk`, `mcp` (공식 SDK, lockfile 고정) |
| 런타임 | Docker Engine 24+, `docker` SDK; 컨테이너 내부 FastAPI |
| Checkpoint | in-memory (dev) → Redis / PostgreSQL (prod) |
| 메모리 & 인덱스 | SQLite + sqlite-vec/FTS5 (dev) → PostgreSQL + pgvector (prod) |
| 관측 | structlog, prometheus-client |
| 품질 | ruff, mypy, pytest (+ testcontainers) |
