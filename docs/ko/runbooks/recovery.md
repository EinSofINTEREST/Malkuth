# 복구

한국어 | **[English](../../en/runbooks/recovery.md)**

장애 후 run, checkpoint, 메모리 인덱스를 복구하는 절차. 규범 규칙:
[05-error-handling.md](../../../.claude/rules/05-error-handling.md).

## Checkpoint 실패

`STOR_001` (저장) 또는 `STOR_002` (복원) 은 run 복구가 위태롭다는 신호다 —
checkpoint 를 남기지 못하는 run 은 재개할 수 없다.

1. checkpointer 백엔드를 먼저 확인한다 (Redis/PostgreSQL 연결, 디스크 여유).
2. 이미 진행 중인 run 은 계속 실행된다 — 잃는 것은 재개 능력뿐이다.
3. 백엔드가 정상화되면 마지막 정상 checkpoint 에서 재개한다:
   `malkuth run resume <run_id>`.

에러를 "지우려고" checkpoint 를 삭제하지 않는다 — 일관된 상태로 돌아가는 유일한
경로다.

## Service Run 정지 (halted)

`GRAPH_005` 는 service run 이 `max_failure_streak` 를 넘겨 **의도적으로** 멈췄다는
뜻이다. crash loop 가 모델 quota 를 무한히 태우지 않게 하는 장치다.

1. 정지된 run 의 에러 코드 분포를 읽는다 — 연속 실패는 대개 단일 원인이 지배한다.
2. 그 원인을 해소한다 (provider quota, MCP 서버, 토폴로지).
3. 마지막 iteration checkpoint 에서 재개한다: `malkuth run resume <run_id>`.

run 은 **다음** iteration 부터 이어진다 — 완료된 iteration 은 반복되지 않는다.

## Run 도중 노드 실패

노드 실패(`GRAPH_002`)는 graph state 를 오염시키지 않는다 — 실패한 노드의 출력은
병합되지 않는다.

1. `malkuth run trace <run_id>` — 실패 노드와 그 `task_id` 를 찾는다.
2. 저장된 요청으로 에이전트를 단독 재현한다: `malkuth replay <task>`.
3. 수정 후 재개한다: `malkuth run resume <run_id>`. 이미 성공한 노드는 다시 실행되지
   않는다.

## 메모리 인덱스 손상

`MEM_003` (인덱싱 적체) 또는 `MEM_004` (검색 실패 / 인덱스 손상).

1. `malkuth_memory_index_lag_seconds` 를 확인한다. 지연이 지속되면 인덱싱 큐가
   빠지지 않는 것이며 대개 embedding provider 가 원인이다.
2. 인덱스가 손상됐거나 embedding 모델이 바뀌었으면 재구축한다:
   `malkuth memory reindex <space>`.
3. 재구축 중에도 검색은 **구 인덱스**로 계속 서빙되고 완료 시 원자적으로 전환된다 —
   재인덱싱이 메모리를 중단시키지 않는다.

## 백업과 복원

| 자산 | 주기 | 비고 |
|---|---|---|
| Checkpointer DB | 일 단위 | run 재개에 필수 |
| 모듈 레지스트리 | Git | `modules/`, `graphs/`, `agents/`, `groups/` 전부 커밋 대상 |
| 설정 | Git | `configs/` |

보존: run 기록·usage 1년, checkpoint 는 완료 후 30일, 로그 30일.
복구 리허설은 분기별로 수행한다 — 검증하지 않은 백업은 백업이 아니다.

## 함께 보기

- [incident-response.md](incident-response.md) — 알림 발생 시 분류
