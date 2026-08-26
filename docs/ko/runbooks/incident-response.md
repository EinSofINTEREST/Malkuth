# 인시던트 대응

한국어 | **[English](../../en/runbooks/incident-response.md)**

Malkuth 알림이 울렸을 때의 대응 절차. 규범 규칙:
[05-error-handling.md](../../../.claude/rules/05-error-handling.md).

## 심각도

| 등급 | 의미 | 예시 |
|---|---|---|
| **P0** | 전체 시스템 장애 | 모든 run 실패, checkpoint 유실 |
| **P1** | 핵심 기능 정지 | 핵심 에이전트 다운, 실패율 >50% |
| **P2** | 성능 저하 | 단일 에이전트 지연, 특정 tool 실패 |
| **P3** | 경미 | 소폭 품질 저하 |

## 처음 5분

1. **Overview** 대시보드를 연다 — 특정 에이전트인가, 특정 그래프인가, 호스트인가?
2. `run_id` 로 로그를 필터링한다 — id 하나가 orchestrator → runtime → agentd →
   protocol 을 관통한다.
3. **에러 코드 분포**를 본다. 접두사가 대응을 결정한다:
   `LLM_*` (provider), `RT_*` (컨테이너), `MCP_*` / `A2A_*` (프로토콜),
   `GRAPH_*` (토폴로지/state), `STOR_*` (checkpoint).

## 알림별 대응

### AgentHighFailureRate

한 에이전트의 태스크 실패율이 10% 를 넘었다.

1. `malkuth agent logs <agent>` — 지배적인 `error_code` 를 읽는다.
2. `LLM_001` (rate limit) → [ModelRateLimited](#modelratelimited) 참조.
   `LLM_005` (max turns) → 프롬프트가 루프에 빠졌을 가능성이 높다, promptset 버전 확인.
   `MCP_003` → tool 이 실패 중이다, Protocol 대시보드 확인.
3. 최근 배포와 상관관계가 있으면 모듈 버전을 롤백한다 — 모듈 버저닝 덕에 즉시 가능하다.

### AgentDown

`malkuth_agent_health == 0` 이 3분간 지속.

1. `malkuth agent inspect <agent>` — manifest 와 실제 로드된 상태를 대조한다.
2. `initialize()` 실패는 컨테이너를 Ready 로 보내지 않는다. 흔한 원인은
   `MCP_001` (필수 MCP 서버 기동 실패) 과 `CFG_002` (secret 키가 어느 스코프에서도
   해석되지 않음) 이다.
3. 해당 에이전트만 재시작한다 — 진행 중 그래프는 checkpoint 에서 재개된다.

### ContainerRestartLoop

10분 내 재시작 5회 초과.

1. `malkuth_container_restarts_total` 의 `reason` 을 확인한다. `RT_003` 은 OOM —
   `runtime.resources.memory` 를 올리거나 동시성을 낮춘다.
2. `RT_001` 반복은 대개 이미지나 entrypoint 문제다 — 컨테이너가 health 를 보고할
   지점까지 도달하지 못한다.
3. 10분 내 5회를 넘기면 runtime 이 에이전트를 **Failed** 로 전환하고 재시도를
   멈춘다. 원인을 고친 뒤 재배포한다.

### ModelRateLimited

Provider 가 요청을 거절하고 있다.

1. 에이전트별 세마포어를 줄여 동시 호출 수를 낮춘다.
2. 그래프가 허용한다면 fallback 모델로 전환한다.
3. `RATE_LIMIT_RETRY` 가 이미 최대 300s 까지 백오프한다 — 알림이 지속된다면
   재시도가 없어서가 아니라 quota 자체가 부족한 것이다.

### ServiceRunStalled

Service run 이 활성인데 30분간 진행이 없다.

1. `malkuth_service_idle_delay_seconds` 를 확인한다 — idle 상한에 머물러 있다면
   입력이 실제로 없는 경우이며 **설계대로 동작 중**이다.
2. 입력이 있는데도 멈춰 있다면 watcher 노드가 조용히 실패하고 있을 가능성이 높다.
   iteration 로그(`iteration` 필드)를 읽고 `is_idle` 판정이 항상 참이 아닌지 확인한다.

## 에스컬레이션

P0/P1 은 호출한다. P2/P3 는 `run_id` 와 에러 코드 분포를 첨부해 이슈로 남긴다 —
이 둘이 있어야 재현이 가능하다.

## 함께 보기

- [recovery.md](recovery.md) — run 복구와 메모리 재인덱싱
