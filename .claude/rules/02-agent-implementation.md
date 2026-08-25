# Agent Implementation Rules

## Core Agent Interface

### Base Agent Contract

모든 에이전트는 다음 계약을 만족해야 한다. 에이전트 컨테이너 내부의 `agentd` 가 이 인터페이스를
Control API 로 노출하고, 오케스트레이터는 오직 Control API 를 통해서만 에이전트를 호출한다.

```python
class BaseAgent(ABC):
    """에이전트 구현의 기본 계약. agentd 가 이 인터페이스를 Control API 로 서빙한다."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Manifest 의 metadata.name 과 일치해야 한다."""

    @abstractmethod
    def card(self) -> AgentCard:
        """A2A AgentCard. manifest 로부터 자동 생성이 기본."""

    @abstractmethod
    async def initialize(self, ctx: AgentContext) -> None:
        """모듈(promptset/skillset)과 프로토콜(MCP/A2A) 초기화. 실패 시 컨테이너 unhealthy."""

    @abstractmethod
    async def invoke(self, task: TaskRequest) -> TaskResult:
        """단일 태스크 실행. 멱등성 보장 필수 (동일 task_id 재호출 안전)."""

    @abstractmethod
    def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """스트리밍 실행 — 구현은 async generator (`async def` + `yield`) 로 작성한다.

        호출자는 `async for event in agent.stream(task)` 로 소비한다 (별도 await 없음).
        07 의 async-first 규칙을 async generator 형태로 적용한 계약이다.
        이벤트 단위: token / tool_call / tool_result / done / error.
        """

    @abstractmethod
    async def health(self) -> HealthStatus:
        """모델 연결, MCP 세션, 의존 모듈 상태 종합."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful shutdown. 진행 중 태스크 drain 후 MCP/A2A 세션 정리."""
```

### Task Models

```python
class TaskRequest(BaseModel):
    task_id: str                          # UUID — 멱등성 키
    run_id: str                           # 소속 graph run (direct 요청은 "direct-" prefix 의 ad-hoc id)
    node_id: str | None                   # 그래프 상 노드 id — direct 요청이면 None
    input: dict[str, Any]                 # state 에서 추출된 입력 / direct 요청 본문
    config: TaskConfig                    # timeout, max_turns 등
    trace: TraceContext                   # 분산 추적 컨텍스트


class TaskResult(BaseModel):
    task_id: str
    status: TaskStatus                    # completed | failed | canceled
    output: dict[str, Any]                # state 로 병합될 출력
    usage: ModelUsage                     # 토큰/비용 집계
    error: MalkuthErrorPayload | None
```

### Rules for All Agents

1. **Cancellation & Timeout**
   - MUST respect `asyncio.CancelledError` — 취소 시 진행 중 tool 호출 정리 후 전파
   - MUST enforce `TaskConfig.timeout_s` (기본 300초) — 초과 시 `TO_001` 로 실패 처리
   - 모델 호출, tool 호출, A2A 호출 각각에 개별 timeout 적용

2. **Statelessness**
   - invoke 간 in-memory 상태 유지 금지 — 모든 지속 데이터는 graph state 로 반환
   - 지속 기억이 필요하면 `AgentContext.memory` 로 선언된 space 에 저장
     ([09-memory-context.md](09-memory-context.md)) — 프로세스 메모리/로컬 파일 금지
   - 캐시는 허용하되 correctness 에 영향을 주지 않아야 함 (컨테이너 재시작 시 소실 전제)

3. **Idempotency**
   - 동일 `task_id` 재호출은 안전해야 함 (재시도/재개 시나리오)
   - 외부 부수효과가 있는 tool 은 task_id 기반 중복 방지 적용

4. **Error Handling**
   - MUST return typed errors (`MalkuthError` payload) — [05-error-handling.md](05-error-handling.md)
   - MUST NOT crash the daemon on task failure — 태스크 실패는 TaskResult 로 보고
   - Uncaught exception 은 agentd 최상위 핸들러에서 `INTERNAL` 카테고리로 변환

5. **Output Discipline**
   - `output` 은 그래프 state schema 와 호환되는 키만 포함
   - 대용량 산출물(파일, 원문)은 output 에 직접 넣지 않고 artifact 저장소 참조로 전달

6. **Peer Equality & Wiring Agnosticism**
   - 에이전트 코드는 자신이 어떤 그래프의 어느 위치에 배선되는지 가정하지 않는다 —
     연결 구조는 배선이 결정하며, 에이전트 간 우열 관계는 존재하지 않는다
   - 그래프 노드 태스크 / peer 위임 태스크 / direct 요청 태스크는 모두 동일한
     TaskRequest 계약으로 처리
   - Mission/service 모드도 가정 금지 — iteration 간 지속 데이터는 graph state 로만

## Agent Manifest

### Manifest Specification

에이전트는 `agents/<name>/manifest.yaml` 로 선언한다. Manifest 는 에이전트의 **유일한 계약
소스**이며, 코드에 하드코딩된 모델명/프롬프트/tool 목록은 금지한다.

```yaml
apiVersion: malkuth/v1
kind: Agent
metadata:
  name: researcher              # 소문자 + 하이픈, 그래프에서 참조하는 id
  version: 0.1.0                # semver — 계약 변경 시 반드시 bump
  group: research               # 소속 그룹 (선택, 최대 1개) — 미선언 시 global 만
  description: 웹 리서치 전담 에이전트

spec:
  model:
    provider: anthropic
    name: claude-sonnet-5
    max_tokens: 8192
    temperature: 0.3

  promptset:
    ref: promptsets/researcher@0.1.0

  skillsets:
    - ref: skillsets/web-search@0.2.0
    - ref: skillsets/summarize@0.1.0

  memory:                       # 컨텍스트 메모리 space — 09 참조
    spaces:
      - ref: memorysets/agent-longterm@0.1.0
        as: longterm

  mcp:                          # 이 에이전트 전용 MCP 서버 — 03 참조
    servers:
      - name: filesystem
        transport: stdio
        command: ["mcp-server-filesystem", "/workspace"]

  a2a:                          # 이 에이전트의 A2A 노출 설정 — 03 참조
    enabled: true
    capabilities:
      streaming: true

  runtime:
    image: malkuth/agent-base:0.1.0   # 커스텀 Dockerfile 있으면 빌드 결과 태그
    resources:
      cpu: "1.0"
      memory: 1Gi
    env_allowlist:              # 컨테이너에 주입 허용되는 env 키 (secrets 포함)
      - ANTHROPIC_API_KEY
    volumes: []                 # 기본 없음 — 필요 시 명시 선언 (아래 격리 규칙 참조)
```

### Manifest Rules

1. **Validation**: 배포 시 pydantic 스키마로 검증 — 미검증 manifest 로 컨테이너 기동 금지
2. **Versioning**: 다음 변경 시 version bump 필수
   - 입력/출력 계약 변경 (minor/major)
   - promptset/skillset ref 변경 (patch 이상)
   - 모델 변경 (minor)
3. **No Hidden Dependencies**: manifest 에 선언되지 않은 모듈/서버/자원 사용 금지
4. **Reference Format**: 모듈 참조는 항상 `{type}/{name}@{version}` — latest 사용 금지
5. **No Hierarchy Declaration**: manifest 에 에이전트 간 우열 관계 선언 금지 —
   연결 구조는 그래프 배선 소관 ([04-module-system.md](04-module-system.md)).
   Peer 호출을 받으려면 `a2a.enabled: true` 필요
6. **Group Membership**: `metadata.group` 은 최대 하나, 존재하는 그룹만 참조.
   미선언 시 global 에만 소속. `group: global` 직접 선언 금지 (배포 검증 차단).
   그룹은 리소스 스코프일 뿐 — 소속이 달라도 배선/호출 규칙은 동일
   ([01-architecture.md](01-architecture.md) Resource Scoping)
7. **MCP Server Schema**: `spec.mcp.servers` 항목의 정식 필드 집합은
   [03-protocol-integration.md](03-protocol-integration.md) 의 서버 선언 스펙을 따른다 —
   stdio/sidecar/external 3 패턴과 `allowed_tools` / `optional` / `auth` /
   `env_allowlist` 전부 manifest 스키마의 정식 필드다 (03 의 예시가 곧 스키마 계약)

## Docker Isolation Rules

### Container Standards

1. **One Agent, One Container**
   - 에이전트 프로세스와 그 MCP stdio 서버들만 같은 컨테이너에 공존 가능
   - 서로 다른 에이전트의 프로세스 동거 금지

2. **Base Image**
   - 모든 에이전트는 `malkuth/agent-base` 에서 시작 (agentd 포함)
   - 커스텀 의존성은 에이전트별 `Dockerfile` 에서 base 를 확장
   - 이미지 태그는 semver — `latest` 태그로 배포 금지

3. **Security**
   ```dockerfile
   # agents/<name>/Dockerfile
   FROM malkuth/agent-base:0.1.0

   # 에이전트별 추가 의존성만 여기서 설치
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt

   COPY src/ /app/agent/
   # base 이미지가 이미 non-root (uid 1000, user: agent) 로 실행
   ```
   - MUST run as non-root user
   - MUST NOT bake secrets into images (빌드 arg 로도 금지)
   - SHOULD use read-only root filesystem + 명시적 writable volume (`/workspace`, `/tmp`)
   - Capability drop: `--cap-drop=ALL` 기본, 필요 capability 만 manifest 로 선언

4. **Resources**
   - CPU/Memory limit 필수 (manifest `runtime.resources`)
   - 미선언 시 프레임워크 기본값 적용 (CPU 1.0 / 1Gi)
   - PID limit 설정 (fork bomb 방지, 기본 256)
   - 소속 그룹의 quota 합계 검증 — 초과 시 기동 거부 (`RT_006`)

5. **Network**
   - 전용 bridge network (`malkuth-net`) 에만 연결
   - 노출 포트는 두 개뿐:
     - **Control port** (agentd, 컨테이너 내부 8080) — runtime layer 만 접근
     - **A2A port** (manifest 로 활성화 시) — allowlist 된 peer 만 접근
   - 호스트 네트워크 모드 금지, 임의 포트 publish 금지
   - Egress: 모델 API / 선언된 MCP 원격 서버 / A2A peer 외 차단이 이상적 (v0.1 은 정책 문서화, 향후 network policy 적용)

6. **Volumes**
   - 기본: 볼륨 없음
   - 필요 시 manifest 에 명시 선언 + 에이전트별 격리 경로만 마운트
   - 에이전트 간 볼륨 공유 금지 (사이드채널 차단)
   - 호스트 민감 경로 (`/var/run/docker.sock` 등) 마운트 절대 금지

### Secrets Injection — Scoped

```
Secret stores:   local(agent) ── group ── global   (3계층, 01 Resource Scoping)
Runtime layer →  env_allowlist 각 키를 local > group > global 순으로 해석
              → (기동 시) docker env 주입 → 컨테이너
```

- Secrets 는 runtime 이 기동 시점에 env 로 주입 — `env_allowlist` 에 있는 키만
- 키 해석은 **local > 소속 group > global** — 가까운 스코프 값이 우선 (shadowing 허용)
- Group 스코프 키는 group.yaml 의 `secrets` 목록에 선언된 것만 멤버에게 제공 —
  비멤버 에이전트는 같은 키를 allowlist 에 넣어도 group 값으로 해석되지 않는다
- 배포 검증: `env_allowlist` 의 각 키가 세 스코프 중 하나에서 해석 가능해야 함
  (실패 시 `CFG_002`)
- 로그에 secret 값 출력 금지 (structlog processor 로 마스킹)
- 에이전트 코드는 `os.environ` 직접 접근 대신 `AgentContext.secrets` 를 통해 접근

## Agent Lifecycle

### Lifecycle States

```
        ┌──────────┐   build   ┌─────────┐   start   ┌──────────┐
        │ Declared │──────────▶│  Built  │──────────▶│ Starting │
        └──────────┘           └─────────┘           └────┬─────┘
                                                          │ initialize 성공
                                    health fail ┌─────────▼─────────┐
                              ┌─────────────────│       Ready       │
                              ▼                 └─────────┬─────────┘
                        ┌───────────┐   재시작 정책        │ drain 요청
                        │ Unhealthy │────────────┐  ┌─────▼─────┐
                        └───────────┘            │  │ Draining  │
                              │ 임계 초과         │  └─────┬─────┘
                              ▼                  │        │ 진행 태스크 완료
                        ┌───────────┐            │  ┌─────▼─────┐
                        │  Failed   │            └─▶│  Stopped  │
                        └───────────┘               └───────────┘
```

### Lifecycle Rules

1. **Build**: 이미지 빌드는 배포 파이프라인에서 — 런타임 중 빌드 금지
2. **Start**: 기동 → `initialize()` → health OK 가 되어야 그래프에 attach
3. **Ready**: health check 주기 실행 (기본 10s 간격, 3회 연속 실패 시 Unhealthy)
4. **Drain**: 새 태스크 수락 중지 → 진행 중 태스크 완료 대기 (기본 30s) → 종료
5. **Stop**: SIGTERM → 30s grace → SIGKILL. `shutdown()` 에서 MCP/A2A 세션 정리
6. **Restart Policy**: Unhealthy 시 exponential backoff 재시작 (1s → 2s → 4s... max 60s),
   10분 내 5회 초과 시 Failed 로 전환하고 알림

### Hot Reload

- **Promptset / Skillset 교체**: `POST /reload` 로 무중단 리로드 지원 (신규 태스크부터 적용)
- **Manifest 변경**: 리로드 불가 — 새 버전으로 재배포 (컨테이너 교체)
- **MCP 서버 목록 변경**: manifest 변경에 해당 — 재배포

## Agent Control API

agentd 가 컨테이너 내부 8080 포트로 서빙하는 표준 API. Runtime layer 외 직접 호출 금지.

```
POST /v1/invoke          # TaskRequest → TaskResult (동기)
POST /v1/stream          # TaskRequest → SSE(TaskEvent 스트림)
GET  /v1/health          # HealthStatus (모델/MCP/모듈 종합)
GET  /v1/card            # A2A AgentCard
POST /v1/cancel/{task_id}# 진행 중 태스크 취소
POST /v1/reload          # promptset/skillset hot reload
POST /v1/drain           # graceful drain 개시
```

### API Rules

1. 모든 응답은 pydantic 모델의 JSON 직렬화 — ad-hoc dict 금지
2. `/invoke` 는 202 + polling 이 아닌 동기 응답 (LangGraph node 실행 모델과 일치).
   장시간 태스크는 `/stream` 사용
3. 인증: runtime 이 발급한 per-agent token 을 `Authorization` 헤더로 요구
4. `/health` 는 무인증 — Docker healthcheck 가 직접 호출

## Execution Loop (agentd internals)

에이전트 내부 실행 루프의 표준 구조:

```python
async def execute(self, task: TaskRequest) -> TaskResult:
    prompt = self.promptset.render(task.node_id, **task.input)
    tools = [*self.skillset.tools(), *self.mcp.tools()]

    async with task_span(task):  # tracing + 로그 컨텍스트
        for turn in range(self.config.max_turns):
            response = await self.model.run(prompt, tools=tools)

            if not response.tool_calls:
                return TaskResult.completed(task, output=response.content)

            results = await self.run_tools(response.tool_calls, task)
            prompt = prompt.extend(response, results)

    raise MalkuthError(category=ErrorCategory.MODEL, code="LLM_005",
                       message="max turns exceeded", retryable=False)
```

### Loop Rules

1. **Max Turns**: tool loop 는 상한 필수 (기본 20) — 무한 루프 방지
2. **Tool Timeout**: 개별 tool 호출 timeout (기본 60s)
3. **Parallel Tools**: 독립 tool call 은 `asyncio.gather` 로 병렬 실행
4. **Usage Tracking**: 매 모델 호출의 토큰 사용량 누적 → TaskResult.usage
5. **Event Emission**: 스트리밍 모드에서 turn 별 tool_call/tool_result 이벤트 발행

## Direct Requests — 인터랙티브 직접 호출

모든 에이전트는 그래프 run 과 무관하게 **직접 요청**을 받을 수 있다.
클라이언트가 특정 에이전트를 지목해 단독 태스크를 실행하거나 스트리밍 대화를 나누는 경로다.

```
Client → Control Plane → Runtime → 대상 에이전트 Control API (/invoke | /stream)
```

### Direct Request Rules

1. **동일 계약**: direct 태스크도 TaskRequest — `node_id=None`,
   `run_id` 는 runtime 이 발급한 `direct-` prefix 의 ad-hoc id
2. **템플릿 선택**: `node_id` 가 없으므로 promptset 의 `default` 템플릿 사용
   ([04-module-system.md](04-module-system.md) 호환성 규칙)
3. **Graph State 불간섭**: direct 태스크는 어떤 graph run 의 state 도 읽거나 쓰지 않는다
4. **동등한 규칙 적용**: timeout / max_turns / usage 집계 / 로깅 — 그래프 태스크와 동일.
   Direct 요청 중에도 allowlist 내 peer A2A 호출 가능
5. **동시성**: direct 태스크와 그래프 태스크는 같은 큐에서 처리 — 에이전트별 동시 실행
   상한(`max_concurrent_tasks`, 기본 4) 공유. 상주 그래프에 붙은 에이전트도
   직접 요청에 응답 가능해야 한다
6. **인터랙티브 세션**: 멀티턴 대화는 클라이언트가 대화 이력을 재전송하는 stateless 방식
   기본 — 에이전트가 세션 상태를 메모리에 쥐지 않는다

## Registering Agents

### Agent Registry

```
agents/
├── planner/
│   ├── manifest.yaml
│   └── Dockerfile          # 선택 — 없으면 base image 그대로 사용
├── researcher/
│   ├── manifest.yaml
│   ├── Dockerfile
│   └── src/
│       └── agent.py        # BaseAgent 커스텀 구현 (선택)
└── writer/
    └── manifest.yaml
```

1. **Declarative Agent** (기본): manifest 만으로 정의 — agentd 의 기본 실행 루프 사용.
   대부분의 에이전트는 promptset + skillset 조합으로 충분해야 한다
2. **Custom Agent**: `src/agent.py` 에 `BaseAgent` 서브클래스 제공 — manifest 의
   `spec.entrypoint: agent.ResearchAgent` 로 지정. 커스텀 구현도 모든 계약 규칙 준수
3. 그래프는 에이전트를 `agents/{name}@{version}` 으로만 참조

## Monitoring per Agent

에이전트 단위로 반드시 수집하는 지표 (구현은 [05-error-handling.md](05-error-handling.md)):

- 태스크 성공/실패율, 태스크 latency (p50/p95)
- 모델 토큰 사용량, tool 호출 횟수/실패율
- 컨테이너 재시작 횟수, health check 실패율
- MCP 세션 상태, A2A 호출 성공률
