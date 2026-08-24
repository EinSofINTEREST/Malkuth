# Error Handling and Monitoring Rules

## Error Handling Principles

### Core Principles

1. **Fail Gracefully**
   - 태스크 실패가 데몬/오케스트레이터를 죽이지 않는다 — 최상위 핸들러에서 수습
   - 에이전트 실패가 graph state 를 오염시키지 않는다 — checkpoint 경계에서 격리
   - 에러를 삼키지 않는다 — 처리하거나 전파하거나, 둘 중 하나

2. **Error Context**
   - 예외는 `raise ... from err` 로 원인 체인 보존
   - 관련 정보 포함 (agent, task_id, run_id, tool, mcp_server 등)

3. **Typed Errors**
   - 카테고리 기반 처리(재시도/라우팅/알림)가 가능하도록 구조화 에러 사용
   - 기계 판독 가능한 error code 포함

## The `MalkuthError` Type

```python
class ErrorCategory(StrEnum):
    # Temporary — retry 가능
    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"

    # Permanent — retry 무의미
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"

    # Protocol
    A2A = "a2a"
    MCP = "mcp"

    # Model
    MODEL = "model"

    # System
    RUNTIME = "runtime"        # 컨테이너/Docker
    GRAPH = "graph"            # 토폴로지/state
    MODULE = "module"          # skillset/promptset/memoryset/registry
    MEMORY = "memory"          # Memory Service / 인덱스
    STORAGE = "storage"
    CONFIG = "config"
    INTERNAL = "internal"


class MalkuthError(Exception):
    """시스템 전반의 구조화 에러 타입."""

    def __init__(
        self,
        category: ErrorCategory,
        code: str,
        message: str,
        *,
        agent: str | None = None,
        task_id: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"[{category}:{code}] {message}")
        self.category = category
        self.code = code
        self.message = message
        self.agent = agent
        self.task_id = task_id
        self.retryable = retryable
        self.details = details or {}

    def payload(self) -> MalkuthErrorPayload:
        """TaskResult / API 응답에 실리는 직렬화 표현."""
        ...
```

- 원인 예외는 `raise MalkuthError(...) from err` 로 연결 — `err` 필드 별도 보관 불필요
- 에러 매칭은 `except MalkuthError as e: if e.category is ...` 또는 code 비교로

## Layer Rules — When to Use `MalkuthError` vs Plain Exceptions

모든 에러 생성을 일률적으로 구조화하지 않고, 레이어별 규칙을 분리한다.

### MUST — `MalkuthError` 로 변환해야 하는 boundary

재시도/라우팅/알림 전략이 카테고리·코드에 의존하는 경계에서는 반드시 `MalkuthError` 로
변환해 전파한다.

| Boundary | Categories |
|---|---|
| `runtime/` 의 컨테이너 제어 결과 (start/stop/health) | `RUNTIME`, `TIMEOUT` |
| `runtime/control.py` 의 Agent Control API 호출 결과 | `NETWORK`, `TIMEOUT`, `RUNTIME` |
| `protocols/a2a/` 의 원격 호출 결과 | `A2A`, `NETWORK`, `TIMEOUT` |
| `protocols/mcp/` 의 세션/tool 호출 결과 | `MCP`, `TIMEOUT` |
| `agentd/executor.py` 의 모델 호출 결과 | `MODEL`, `RATE_LIMIT`, `TIMEOUT` |
| `orchestrator/` 의 node 실행/state 병합 결과 | `GRAPH` |
| `modules/registry.py` 의 ref 해석/로드 결과 | `MODULE`, `CONFIG` |
| `orchestrator/checkpoint.py` 의 저장/복원 결과 | `STORAGE` |
| `memory/` 의 append/search/recall 결과 | `MEMORY`, `STORAGE` |

### MAY — plain exception 유지가 적절한 레이어

- **순수 유틸/헬퍼 함수**: `ValueError`, `KeyError` 등 표준 예외로 충분.
  boundary 를 통과할 때 상위에서 변환된다
- **pydantic 검증**: `ValidationError` 그대로 두고, boundary(배포 검증, API 입력)에서
  `VALIDATION` 카테고리로 변환
- **skill 구현 내부**: skill 은 도메인 예외를 그대로 던진다 —
  agentd 의 tool 실행 boundary 가 `MCP_003`/`SKILL` 계열로 변환·기록

### 예시 — boundary 변환 패턴

```python
# protocols/mcp/client.py (boundary — MalkuthError 로 변환)
async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
    try:
        return await self._session.call_tool(name, args)
    except McpError as err:
        raise MalkuthError(
            category=ErrorCategory.MCP,
            code="MCP_003",
            message=f"tool call failed: {name}",
            agent=self.agent_name,
            retryable=False,
        ) from err
    except ConnectionError as err:
        raise MalkuthError(
            category=ErrorCategory.MCP,
            code="MCP_004",
            message="mcp transport disconnected",
            agent=self.agent_name,
            retryable=True,
        ) from err
```

## Error Codes

시스템 전반에서 일관된 코드를 사용한다:

```
NET_001: Connection refused / DNS 실패
NET_002: Connection timeout
TO_001:  Task timeout (TaskConfig.timeout_s 초과)
TO_002:  Tool timeout
TO_003:  Node timeout (orchestrator 기준)

LLM_001: Provider rate limited
LLM_002: Context length exceeded
LLM_003: Provider server error
LLM_004: Invalid/unparseable model response
LLM_005: Max turns exceeded

A2A_001: Task 제출 실패
A2A_002: Peer 도달 불가
A2A_003: Task 거부/실패 (callee 측)
A2A_004: Connection allowlist 위반
A2A_005: 호출 깊이 초과

MCP_001: 서버 기동/initialize 실패
MCP_002: Tool 미존재
MCP_003: Tool 실행 실패
MCP_004: Transport 단절

RT_001:  컨테이너 기동 실패
RT_002:  컨테이너 unhealthy
RT_003:  OOM killed
RT_004:  이미지 빌드/풀 실패
RT_005:  Drain timeout

GRAPH_001: 토폴로지 검증 실패
GRAPH_002: Node 실행 실패 (에이전트 에러 wrapping)
GRAPH_003: State schema 불일치 / 병합 실패
GRAPH_004: Max iterations 초과 (mission)
GRAPH_005: Service iteration 연속 실패 임계 초과 — run 정지

MOD_001: 모듈 ref 해석 실패
MOD_002: 모듈 버전/의존성 충돌
MOD_003: 모듈 스키마(yaml) 검증 실패
MOD_004: Promptset 변수 검증 실패

MEM_001: Memory space 미선언 / access 거부
MEM_002: Memory 저장 실패
MEM_003: 인덱싱 실패 누적 / 재인덱싱 필요
MEM_004: 검색 실패 / 인덱스 손상

VAL_001: 필수 필드 누락
VAL_002: 필드 형식 오류

STOR_001: Checkpoint 저장 실패
STOR_002: Checkpoint 복원 실패
STOR_003: Registry 저장소 오류

CFG_001: 설정 파싱/검증 실패
```

## Retry Logic

### Retry Policy

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    initial_delay_s: float
    max_delay_s: float
    multiplier: float = 2.0
    jitter: bool = True
    retryable_categories: tuple[ErrorCategory, ...] = ()


NETWORK_RETRY = RetryPolicy(
    max_attempts=3, initial_delay_s=1, max_delay_s=30,
    retryable_categories=(ErrorCategory.NETWORK, ErrorCategory.TIMEOUT),
)

RATE_LIMIT_RETRY = RetryPolicy(
    max_attempts=5, initial_delay_s=10, max_delay_s=300,
    retryable_categories=(ErrorCategory.RATE_LIMIT,),
)
```

### Retry Rules

1. 구현은 `tenacity` 사용 — 수제 재시도 루프 금지
2. `MalkuthError.retryable == False` 면 즉시 중단 — 카테고리가 retryable 목록에 있어도
3. 재시도 전 대기 중에도 cancellation 존중 (`asyncio.CancelledError` 즉시 전파)
4. 재시도 시 WARN 로그: `attempt`, `max_attempts`, `delay_ms` 필드 필수
5. **부수효과가 있는 호출**(external tool)은 멱등성이 확보된 경우에만 자동 재시도

### Retry Layering — 이중 재시도 방지

재시도는 **한 계층에서만** 수행한다:

| 호출 | 재시도 주체 |
|---|---|
| 모델 API 호출 | agentd (provider SDK 재시도는 비활성화) |
| MCP tool 호출 | agentd — 단 `MCP_004` (transport) 는 재연결 후 1회만 |
| A2A 호출 | caller 에이전트 |
| Node 실행 전체 | orchestrator — node 별 `retry` 설정 시에만, 에이전트 내부 재시도와 중복 주의 |
| 컨테이너 재시작 | runtime (lifecycle 정책) |

### Circuit Breaker

외부 의존 대상별로 circuit breaker 를 적용한다:

- 모델 provider (에이전트별)
- MCP external/sidecar 서버 (서버별)
- A2A peer (edge 별)
- Agent Control API (runtime → 에이전트, 에이전트별)

```python
breaker = CircuitBreaker(max_failures=5, reset_timeout_s=60)

async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
    if not breaker.can_attempt():
        raise MalkuthError(category=..., code=..., message="circuit open",
                           retryable=True)
    ...
```

Open 상태 전환/복구 시 WARN 로그 + `malkuth_circuit_state` 메트릭 갱신.

## Logging

### Structured Logging

`structlog` 기반 구조화 로깅을 사용한다.
로그 메시지는 **반드시 영어**로 작성한다. (주석·문서는 한국어 허용, 로그 msg 문자열은 영어)

```python
# Good
log.info(
    "agent task completed",
    agent="researcher",
    task_id=task.task_id,
    run_id=task.run_id,
    duration_ms=elapsed_ms,
)

log.error(
    "mcp server initialization failed",
    agent="researcher",
    mcp_server="filesystem",
    error_code="MCP_001",
    exc_info=err,
)

# Bad — 한국어 로그 메시지 금지
log.error("MCP 서버 기동 실패", ...)
# Bad — f-string 보간 금지, 필드로 분리
log.info(f"task {task_id} done in {elapsed}ms")
```

### Log Levels

| Level | 사용 시점 | 예시 시나리오 |
|-------|-----------|--------------|
| **DEBUG** | 개발/트러블슈팅용 내부 상세 | tool 호출 인자, 템플릿 렌더 결과 크기, edge 조건 평가값 |
| **INFO**  | 운영자가 확인하고 싶은 정상 마일스톤 | run 시작/완료, 에이전트 Ready, 그래프 배포 완료, checkpoint 저장 |
| **WARN**  | 예상 외지만 gracefully 처리됨 | 재시도, MCP 재연결, optional 서버 기동 실패, drain timeout 임박 |
| **ERROR** | 작업 실패 — 다른 run 은 계속 가능 | 태스크 실패, node 실패, tool 실패, checkpoint 저장 실패 |
| **FATAL** | 복구 불가 — 프로세스 종료 | 설정 파싱 실패, Docker daemon 접속 불가 (기동 시) |

**빠른 판단 기준:**
- 운영자가 알림을 받아야 하면: **ERROR** 이상
- 처리됐지만 운영자가 알아야 하면: **WARN**
- 정상 동작 확인: **INFO**
- 개발자 트레이싱: **DEBUG**

### Field Naming Conventions

모든 구조화 필드 키는 **snake_case**. 아래 표준 이름을 반드시 사용한다.

| Field Key        | Type   | 설명 |
|-----------------|--------|------|
| `agent`         | str    | 에이전트 이름 (manifest metadata.name) |
| `agent_version` | str    | 에이전트 버전 |
| `graph`         | str    | 그래프 이름 |
| `run_id`        | str    | Graph run UUID |
| `task_id`       | str    | 태스크 UUID |
| `node_id`       | str    | 그래프 노드 id |
| `a2a_caller`    | str    | A2A 호출자 에이전트 |
| `a2a_callee`    | str    | A2A 피호출자 에이전트 |
| `a2a_task_id`   | str    | A2A task id |
| `mcp_server`    | str    | MCP 서버 이름 |
| `tool`          | str    | tool 이름 (네임스페이스 포함) |
| `skillset`      | str    | 스킬셋 ref |
| `promptset`     | str    | 프롬프트셋 ref |
| `module_ref`    | str    | 모듈 참조 문자열 (`type/name@version`) |
| `memory_space`  | str    | 메모리 space 별칭/이름 |
| `model`         | str    | 모델 이름 |
| `provider`      | str    | 모델 provider |
| `container_id`  | str    | Docker 컨테이너 id (short) |
| `image`         | str    | 이미지 태그 |
| `error_code`    | str    | 에러 코드 상수 (예: `"MCP_003"`) |
| `status`        | str    | 결과 상태 (`completed`/`failed`/...) |
| `duration_ms`   | int    | 경과 시간 (밀리초) |
| `delay_ms`      | int    | 재시도 backoff 대기 (밀리초) |
| `attempt`       | int    | 현재 시도 횟수 (1-based) |
| `max_attempts`  | int    | 최대 시도 횟수 |
| `input_tokens`  | int    | 입력 토큰 수 |
| `output_tokens` | int    | 출력 토큰 수 |
| `turn`          | int    | tool loop 회차 |
| `iteration`     | int    | Service run 의 iteration 회차 |
| `mode`          | str    | Run 모드 (`mission`/`service`/`direct`) |
| `port`          | int    | 포트 |

**규칙:**
- 표에 없는 컴포넌트 특화 키는 snake_case 로 추가 가능 (예: `checkpoint_id`, `edge`)
- 의미 중복 키 생성 금지 (예: `"agent_name"` 대신 `"agent"`)
- 시간 값은 밀리초 int, 키는 `duration_ms` / `delay_ms`
- 불리언 플래그는 긍정형 (예: `retryable`, `is_replica`)
- secrets/토큰 값은 절대 로그 금지 — structlog processor 마스킹 적용

### Per-Component Required Fields

| 컴포넌트 | 모든 로그에 필수 |
|---|---|
| `orchestrator/` | `graph`, `run_id` (+node 실행 로그는 `node_id`, `agent` / service run 로그는 `iteration`) |
| `runtime/` | `agent` (+컨테이너 조작 로그는 `container_id`, `image`) |
| `protocols/a2a/` | `a2a_caller`, `a2a_callee` (+task 로그는 `a2a_task_id`) |
| `protocols/mcp/` | `agent`, `mcp_server` (+tool 로그는 `tool`, `duration_ms`) |
| `agentd/` | `agent`, `task_id` (+모델 호출은 `model`, `input_tokens`, `output_tokens`) |
| `modules/` | `module_ref` |

### Log Context Binding

컴포넌트 초기화/태스크 진입 시 bound logger 로 반복 필드 중복을 방지한다:

```python
log = structlog.get_logger().bind(
    agent=self.name,
    run_id=task.run_id,
    task_id=task.task_id,
)
```

### Log Storage

1. **Console**: 개발 환경 pretty 모드
2. **File/Stdout JSON**: 컨테이너 stdout 으로 JSON — Docker logging driver 가 수집
3. **Centralized**: 프로덕션은 Loki 등으로 집계, `run_id` 검색 가능해야 함. 30일 보존

## Monitoring

### Metrics Collection

Prometheus (`prometheus-client`) 사용. 표준 메트릭:

```python
# Task metrics
malkuth_agent_tasks_total{agent, graph, status}          # Counter
malkuth_agent_task_duration_seconds{agent, graph}        # Histogram

# Model metrics
malkuth_model_requests_total{agent, provider, model, status}
malkuth_model_tokens_total{agent, model, direction}      # direction: input|output

# Tool / protocol metrics
malkuth_tool_calls_total{agent, source, tool, status}    # source: skillset|mcp
malkuth_mcp_tool_calls_total{agent, server, tool, status}
malkuth_a2a_calls_total{caller, callee, status}

# Runtime metrics
malkuth_containers_running{agent}                        # Gauge
malkuth_container_restarts_total{agent, reason}
malkuth_agent_health{agent}                              # Gauge: 1 healthy / 0 unhealthy

# Orchestrator metrics
malkuth_runs_active{graph, mode}                         # Gauge — mode: mission|service
malkuth_runs_total{graph, mode, status}
malkuth_node_duration_seconds{graph, node_id}
malkuth_checkpoint_operations_total{operation, status}

# Service run metrics
malkuth_service_iterations_total{graph, status}          # Counter — iteration 단위
malkuth_service_idle_delay_seconds{graph}                # Gauge — 현재 idle backoff

# Memory metrics ([09-memory-context.md](09-memory-context.md))
malkuth_memory_operations_total{space, op, status}       # op: append|search|recall
malkuth_memory_search_duration_seconds{space}
malkuth_memory_entries{space}                            # Gauge
malkuth_memory_index_lag_seconds{space}

# Circuit breaker
malkuth_circuit_state{target}                            # Gauge: 0 closed / 1 open / 2 half
```

### Health Checks

```python
class HealthStatus(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    components: dict[str, ComponentHealth]   # model, mcp:{server}, a2a, modules, memory
    checked_at: datetime
```

Health check 대상:
- 에이전트: 모델 연결(경량 ping 또는 최근 성공 기록), MCP 세션별 상태, A2A 서버 liveness
- 오케스트레이터: checkpointer 연결, Docker daemon 연결
- Degraded 기준: 지연 임계 초과 또는 optional 컴포넌트 실패

### Alerting Rules

```yaml
groups:
  - name: malkuth
    interval: 30s
    rules:
      - alert: AgentHighFailureRate
        expr: |
          rate(malkuth_agent_tasks_total{status="failed"}[5m]) /
          rate(malkuth_agent_tasks_total[5m]) > 0.1
        for: 5m
        labels: {severity: warning}
        annotations:
          summary: "Agent {{ $labels.agent }} failure rate > 10%"

      - alert: AgentDown
        expr: malkuth_agent_health == 0
        for: 3m
        labels: {severity: critical}
        annotations:
          summary: "Agent {{ $labels.agent }} unhealthy for 3m"

      - alert: ContainerRestartLoop
        expr: increase(malkuth_container_restarts_total[10m]) > 5
        labels: {severity: critical}
        annotations:
          summary: "Agent {{ $labels.agent }} restart loop"

      - alert: ModelRateLimited
        expr: |
          rate(malkuth_model_requests_total{status="rate_limited"}[5m]) > 1
        for: 5m
        labels: {severity: warning}

      - alert: CheckpointFailures
        expr: |
          rate(malkuth_checkpoint_operations_total{status="error"}[5m]) > 0
        for: 5m
        labels: {severity: critical}
        annotations:
          summary: "Checkpoint failures — run recovery at risk"

      - alert: ServiceRunStalled
        expr: |
          malkuth_runs_active{mode="service"} > 0
          and increase(malkuth_service_iterations_total[30m]) == 0
        labels: {severity: critical}
        annotations:
          summary: "Service graph {{ $labels.graph }} made no progress for 30m"

      - alert: ServiceRunHalted
        expr: increase(malkuth_service_iterations_total{status="halted"}[10m]) > 0
        labels: {severity: critical}
        annotations:
          summary: "Service graph {{ $labels.graph }} halted (GRAPH_005 failure streak)"
```

### Dashboards

Grafana 대시보드 구성:

1. **Overview**: active runs, 에이전트별 태스크 성공/실패율, 토큰 사용량, 컨테이너 상태
2. **Agent Detail**: 태스크 latency 분포, tool 호출 breakdown, 모델 사용량, 재시작 이력
3. **Protocol**: A2A 호출 매트릭스 (caller×callee), MCP 서버별 tool 성공률/지연
4. **Graph**: node 별 latency, run 소요 시간 분포, 실패 node 랭킹
5. **System**: 호스트 CPU/Memory, 컨테이너 리소스 사용률, Docker 디스크

## Incident Response

### On-Call Procedures

1. **Alert Response**
   - Grafana 대시보드 확인 → 영향 범위 파악 (특정 에이전트 / 그래프 전체 / 인프라)
   - `run_id` 로 실패 run 의 로그 추적
   - 에러 코드 분포 확인 (LLM_* 인지 RT_* 인지에 따라 대응이 다름)

2. **Mitigation Steps**
   - 특정 에이전트 장애: 해당 에이전트만 재시작 (그래프는 checkpoint 에서 재개)
   - Provider rate limit: 세마포어 축소, 필요 시 fallback 모델 전환
   - MCP 서버 불안정: `optional` 전환 또는 해당 skillset 비활성 버전으로 롤백
   - 토폴로지 문제: 이전 그래프 버전으로 롤백 (모듈 버저닝 덕에 즉시 가능)
   - Service run 정지 (GRAPH_005): 에러 코드 분포로 원인 해소 확인 후
     `malkuth run resume <run_id>` 로 마지막 iteration checkpoint 에서 재개

3. **Escalation**
   - P0 (Critical): 전체 run 실패, checkpoint 유실
   - P1 (High): 핵심 에이전트 다운, >50% 실패율
   - P2 (Medium): 단일 에이전트 성능 저하, 특정 tool 실패
   - P3 (Low): 경미한 품질 저하

### Debugging Tools

1. **Run Tracing**
   - `run_id` 하나로 orchestrator → runtime → agentd → protocol 로그 전체 연결
   - TraceContext 를 A2A 호출까지 전파

2. **Replay Capability**
   - Checkpoint 기반 특정 node 부터 재실행
   - 태스크 입력(TaskRequest) 저장 → 동일 입력으로 에이전트 단독 재현 (`malkuth replay`)

3. **Debug Commands**
   ```
   malkuth status                        # 그래프/에이전트 상태 요약
   malkuth agent logs <agent>            # 에이전트 컨테이너 로그
   malkuth agent inspect <agent>         # manifest + 실제 로드 상태 비교
   malkuth run trace <run_id>            # run 의 node 실행 타임라인
   malkuth run resume <run_id>           # 마지막 checkpoint 에서 재개
   ```

## Data Integrity

### Consistency Checks

1. Graph run 기록 ↔ checkpoint 존재 여부 대조 (orphan 감지)
2. 배포된 그래프의 모듈 ref ↔ registry 실재 여부 정기 검증
3. 컨테이너 실재 ↔ runtime 이 아는 에이전트 목록 대조 (유령 컨테이너 정리)

### Backup and Recovery

1. **Backup**
   - Checkpointer DB (Postgres/Redis): 일 단위 백업
   - Module registry: git 으로 버전 관리 (modules/, graphs/, agents/ 전부 커밋 대상)
   - Config: git 버전 관리

2. **Recovery**
   - 복구 절차 runbook 문서화 (`docs/runbooks/`)
   - 분기별 복구 리허설

3. **Retention**
   - Run 기록/usage 집계: 1년
   - Checkpoint: run 완료 후 30일 (재현 필요 기간)
   - 로그: 30일
