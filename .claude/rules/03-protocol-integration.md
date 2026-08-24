# Protocol Integration Rules — A2A and MCP

## Core Principle: Per-Agent Isolation

프로토콜 자원은 **에이전트 단위로 격리**된다. 이것이 Malkuth 프로토콜 계층의 제1원칙이다.

1. **소유권**: 모든 A2A 엔드포인트와 MCP 서버/세션은 정확히 하나의 에이전트에 속한다
2. **공유 금지**:
   - 여러 에이전트가 하나의 MCP 서버 프로세스를 공유하는 것 금지
   - 에이전트 컨테이너 밖의 "글로벌 MCP 허브" / "공용 tool 게이트웨이" 금지
   - A2A 엔드포인트를 우회하는 에이전트 간 직통 채널 금지
3. **선언 필수**: 에이전트가 사용하는 모든 프로토콜 자원은 manifest 에 선언되어야 한다
4. **격리 경계 = 컨테이너 경계**: MCP 서버는 소유 에이전트의 컨테이너 내부(stdio) 또는
   해당 에이전트 전용 사이드카로만 실행된다

### Why

- 에이전트별 권한 경계가 명확해진다 — 에이전트 A 의 filesystem MCP 가 B 의 데이터를 볼 수 없음
- 에이전트를 그래프에서 분리하면 그 프로토콜 자원도 함께 정리된다 (누수 없음)
- 장애 격리 — 한 에이전트의 MCP 서버 crash 가 다른 에이전트에 전파되지 않음
- 모듈형 연결/분리 목표와 일치 — 에이전트는 자신의 자원을 전부 들고 이동하는 단위

## A2A (Agent2Agent) Integration

### A2A Server Rules

각 에이전트는 자신의 A2A 서버를 컨테이너 내부에서 직접 노출한다.

1. **AgentCard**
   - manifest 로부터 자동 생성이 기본 (`name`, `description`, `capabilities`, skills 요약)
   - Card 의 skill 목록은 실제 로드된 skillset 과 일치해야 함 — 수동 작성 금지
   - `GET /v1/card` (Control API) 와 A2A well-known 경로 양쪽에서 동일 내용 제공

2. **Port Assignment**
   - Runtime 이 `protocols.a2a.port_range` 에서 에이전트별 포트 할당
   - 에이전트 코드는 포트를 하드코딩하지 않고 `AgentContext.a2a_port` 사용

3. **Task Lifecycle Mapping**
   - A2A task 상태를 내부 TaskStatus 로 매핑:
     `submitted → working → completed | failed | canceled`
   - 스트리밍 지원 시 A2A 이벤트 ↔ 내부 TaskEvent 1:1 매핑 유지

### A2A Client Rules — Connection Allowlist

에이전트 간 A2A 호출은 **그래프 config 의 `connections` 에 선언된 쌍만** 허용된다.

```yaml
# graphs/research-pipeline.yaml (발췌)
spec:
  connections:
    - caller: researcher
      callee: planner
      # researcher → planner 방향 호출만 허용. 역방향은 별도 선언 필요.
```

1. **Enforcement (이중 방어)**
   - Caller 측: agentd 가 allowlist 에 없는 callee 로의 호출을 `A2A_004` 로 거부
   - Callee 측: runtime 이 발급한 per-edge token 검증 — allowlist 외 호출자 차단
2. **Discovery**: 에이전트는 peer 의 주소를 직접 알지 못한다 —
   `AgentContext.peers` 로 주입된 (allowlist 기반) 목록만 조회 가능
3. **No Transitive Calls**: A 가 B 를 호출하고 B 가 C 를 호출하는 것은 각각의 선언이 있을 때만
   가능 — caller 의 권한이 전파되지 않는다
4. **Timeout & Retry**: A2A 호출은 timeout 필수 (기본 120s), 재시도는 caller 의
   RetryPolicy 를 따름 — [05-error-handling.md](05-error-handling.md)
5. **Depth Limit**: A2A 호출 체인 깊이 상한 (기본 3) — `TraceContext.depth` 로 전파/검증,
   초과 시 `A2A_005` (순환 위임 방지)

### When to Use A2A vs Graph Edge

| 상황 | 사용 |
|---|---|
| 워크플로의 정해진 다음 단계로 데이터 전달 | **Graph edge** (state 경유) |
| 실행 도중 다른 에이전트에게 부분 작업 위임 후 결과를 이어서 사용 | **A2A call** |
| 다른 에이전트에게 질의만 하고 즉시 응답 필요 | **A2A call** |
| 산출물을 여러 후속 노드가 소비 | **Graph edge** (state 경유) |

A2A 는 위임/질의용이다. 파이프라인 데이터 흐름을 A2A 로 구현하는 것은 안티패턴 —
checkpoint/재개가 불가능해진다.

## MCP (Model Context Protocol) Integration

### MCP Server Declaration

에이전트가 사용할 MCP 서버는 manifest 의 `spec.mcp.servers` 에 선언한다.

```yaml
spec:
  mcp:
    servers:
      # 패턴 1 — stdio: 에이전트 컨테이너 내부에서 자식 프로세스로 실행 (기본)
      - name: filesystem
        transport: stdio
        command: ["mcp-server-filesystem", "/workspace"]
        env_allowlist: []                # 서버 프로세스에 전달할 env 키

      # 패턴 2 — sidecar: 에이전트 전용 사이드카 컨테이너 (HTTP 계열 transport)
      - name: browser
        transport: streamable-http
        sidecar:
          image: mcp/playwright:1.2.0
          resources: {cpu: "0.5", memory: 512Mi}
        # URL 은 runtime 이 사이드카 기동 후 주입 — 수동 URL 기입 금지

      # 패턴 3 — external: 외부 원격 MCP 서버 (명시적 URL)
      - name: corp-search
        transport: streamable-http
        url: https://mcp.internal.example.com/search
        auth:
          type: bearer
          token_env: CORP_SEARCH_TOKEN   # env_allowlist 에도 등록 필요
```

### MCP Rules

1. **Placement**
   - stdio 서버: 소유 에이전트 컨테이너 내부에서만 실행
   - sidecar 서버: 소유 에이전트와 1:1 — 다른 에이전트가 같은 사이드카에 접속 금지.
     사이드카 lifecycle 은 소유 에이전트를 따름 (에이전트 stop 시 함께 정리)
   - external 서버: 접속은 허용하되, 자격증명은 에이전트별로 분리 발급 권장

2. **Session Management**
   - `initialize()` 에서 세션 수립, 실패 시 에이전트 unhealthy (`MCP_001`)
   - 세션 단절 감지 시 자동 재연결 (backoff), 재연결 실패 누적 시 unhealthy
   - `shutdown()` 에서 세션/자식 프로세스 정리 — 좀비 프로세스 금지

3. **Tool Namespacing**
   - MCP tool 은 `mcp__{server}__{tool}` 로 네임스페이스 — skillset tool 과 충돌 방지
   - 동일 서버 이름 중복 선언 금지 (manifest 검증에서 차단)

4. **Tool Filtering**
   - 서버가 노출하는 tool 전체를 무조건 바인딩하지 않는다 —
     `allowed_tools` 로 필요한 tool 만 선별 가능 (기본: 전체 허용, 명시 시 allowlist)
   ```yaml
   - name: filesystem
     transport: stdio
     command: ["mcp-server-filesystem", "/workspace"]
     allowed_tools: [read_file, list_directory]   # write 계열 차단
   ```

5. **Security**
   - stdio `command` 는 이미지에 설치된 실행 파일만 — 셸 문자열 (`sh -c`) 금지
   - 서버 프로세스에 전달되는 env 는 `env_allowlist` 로 제한
   - MCP 서버가 반환한 컨텐츠는 **untrusted input** — 프롬프트에 주입 시 경계 표시,
     서버 응답 내 지시문을 시스템 지시로 승격 금지

6. **Resources & Prompts**
   - MCP resources/prompts 기능 사용 시에도 동일한 격리·선언 규칙 적용
   - Resource 구독(subscription)은 태스크 lifecycle 내에서만 — 태스크 종료 시 해제

### MCP Startup Sequence

```
agentd bootstrap
  ├─ 1. manifest 로드 + 검증
  ├─ 2. promptset / skillset 로드
  ├─ 3. MCP 서버 기동 (stdio spawn / sidecar 대기 / external 접속)
  │     └─ 각 서버 initialize + tool 목록 수집 (timeout: 15s/서버)
  ├─ 4. tool registry 구성 (skillset + mcp, 네임스페이스 충돌 검사)
  ├─ 5. A2A 서버 기동 (enabled 시)
  └─ 6. health Ready 보고
```

- 3단계에서 하나라도 실패하면 Ready 로 전환하지 않는다 (부분 기동 금지)
- 단, manifest 에 `optional: true` 로 표시된 서버는 실패해도 기동 지속 + WARN 로그

## Protocol Error Mapping

프로토콜 계층에서 발생하는 에러는 boundary 에서 `MalkuthError` 로 변환한다.
상세 규칙은 [05-error-handling.md](05-error-handling.md).

| 상황 | Category | Code | Retryable |
|---|---|---|---|
| MCP 서버 기동 실패 | `mcp` | `MCP_001` | 아니오 (설정 문제) |
| MCP tool 미존재 | `mcp` | `MCP_002` | 아니오 |
| MCP tool 실행 실패 | `mcp` | `MCP_003` | tool 에 따라 |
| MCP transport 단절 | `mcp` | `MCP_004` | 예 (재연결 후) |
| A2A task 제출 실패 | `a2a` | `A2A_001` | 예 |
| A2A peer 도달 불가 | `a2a` | `A2A_002` | 예 |
| A2A task 거부/실패 | `a2a` | `A2A_003` | 아니오 |
| A2A allowlist 위반 | `a2a` | `A2A_004` | 아니오 |
| A2A 호출 깊이 초과 | `a2a` | `A2A_005` | 아니오 |

## Version Pinning

1. **SDK Versions**: `a2a-sdk`, `mcp` 패키지는 lockfile 로 고정 — 프로토콜 SDK 의
   자동 업그레이드 금지 (breaking change 빈도 높음)
2. **Protocol Version Negotiation**
   - MCP: initialize 시 서버가 제시한 protocol version 기록 (로그 + 메트릭)
   - 지원 범위 밖 버전이면 `MCP_001` 로 실패 — silent degradation 금지
3. **Sidecar Images**: MCP 사이드카 이미지는 semver 태그 고정 — `latest` 금지

## Observability Requirements

프로토콜 계층의 모든 원격 호출은 다음을 남긴다:

```python
log.info(
    "mcp tool call completed",
    agent=self.name,
    mcp_server="filesystem",
    tool="read_file",
    duration_ms=elapsed_ms,
    task_id=task.task_id,
)
```

- **A2A**: caller, callee, a2a_task_id, duration_ms, status
- **MCP**: mcp_server, tool, duration_ms, status
- 메트릭: `malkuth_a2a_calls_total{caller,callee,status}`,
  `malkuth_mcp_tool_calls_total{agent,server,tool,status}`
- Trace: TraceContext 를 A2A 호출에 전파 — run 전체를 단일 trace 로 연결
