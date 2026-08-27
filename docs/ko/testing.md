# 테스트 전략

한국어 | **[English](../en/testing.md)**

비결정적인 모델 주변에서 Malkuth 가 결정성을 유지하는 방법. 규범 규칙:
[06-testing.md](../../.claude/rules/06-testing.md).

## 원칙

1. **테스트는 실제 LLM 을 호출하지 않는다** — 스크립트된 `FakeModel` (또는 기록된
   cassette) 로 대체. CI 에는 의도적으로 provider API key 가 없으며, key 가 필요한
   테스트는 버그다.
2. **외부 서비스 의존 금지** — Docker fixture (testcontainers) 또는 fake 만 사용.
3. **결정적 + 병렬 실행 가능** — 시간 의존 로직(backoff, health 주기, service idle)은
   clock 주입으로 테스트, `sleep` 금지.
4. 임베딩은 결정적 해시 기반 fake 사용 — 임베딩 API 호출 금지.

## 테스트 피라미드와 배치

```
tests/
├── unit/           # 외부 의존 없음 — src/malkuth 구조 미러링          (~70%)
├── integration/    # Docker / 실제 MCP 서버 fixture                    (~25%)
├── e2e/            # 전체 compose 스택, fake LLM provider 컨테이너      (~5%)
└── fixtures/       # FakeModel, fake MCP 서버, builder, 테스트 yaml
```

- 마커: `@pytest.mark.integration`, `@pytest.mark.e2e`
- `make test` 는 unit 만 실행; `make test-integration` / `make test-e2e` 는 분리 실행
- E2E 는 CI 에서 nightly (`ci-nightly.yml`) — PR gate 아님

## 필수 커버 영역

| 영역 | 초점 |
|---|---|
| core | manifest / topology / group 스키마 검증, 스코프 해석 |
| orchestrator | config → StateGraph 빌드, 라우팅, state 병합, mission/service 모드 규칙 |
| protocols | `MalkuthError` 변환, A2A allowlist, tool 네임스페이싱 |
| modules | ref 파싱, 스키마 스냅샷(스킬셋), 렌더 골든(프롬프트셋) |
| memory | space ACL, 별칭 해석 (local > group > global), 하이브리드 검색 병합, recall 예산 |
| agentd | tool loop 상한, cancellation 정리, direct 태스크 처리 |
| runtime | 컨테이너 lifecycle, quota 거부 (`RT_006`), drain 시맨틱 |

Service 그래프는 추가로: iteration 누적, idle backoff 진행, drain 중 iteration 재개,
연속 실패 정지(`GRAPH_005`) 시나리오가 필수입니다.

## 품질 게이트 (CI 강제)

```bash
make lint          # ruff check + ruff format --check
make typecheck     # mypy — src/malkuth/core 는 strict
make test          # pytest unit + 커버리지 ≥ 70% (미달 시 실패)
make test-integration
```

커버리지: **강제 게이트는 하나** — `src/malkuth` 전체 기준 70% 이상, `make test` 의
pytest-cov `--cov-fail-under=70` 로 강제. 핵심 경로 90%+ 와 에러 변환 경로 100% 는
리뷰 목표치이며 별도 CI 게이트가 아닙니다.
체크 이름과 머지 게이트 연결: [ci/status-checks.md](ci/status-checks.md).

## 알려진 한계

이 스위트가 **증명하지 않는 것** — 초록불을 완전함으로 오해하지 않도록:

- **실제 모델은 한 번도 호출하지 않습니다.** 유닛은 `FakeModel`, E2E 는 결정적
  fake provider 컨테이너를 씁니다. 실 provider 에서만 드러나는 동작(토큰 한도,
  스트리밍 특이사항, provider 측 rate limit)은 여기서 다루지 않습니다.
- **실제 embedding API 도 쓰지 않습니다.** 메모리 테스트는 `HashEmbedder` 를
  쓰는데, 결정적이지만 의미 유사도를 모델링하지는 않습니다. 따라서 recall 의
  **순위 품질**은 측정되지 않고, 병합·문턱·예산의 동작만 검증됩니다.
- **SDK 바인딩은 검증되지만, 프로세스 안에서만입니다.** `mcp` 와 `a2a-sdk`
  바인딩을 직접 태웁니다 — MCP 는 참조 서버 프로세스로, A2A 는 SDK 자신의 서버
  라우트를 ASGI 로. 둘 다 실제 SDK 를 지나지만 **컨테이너 경계는 넘지
  않습니다** (아래 Docker 항목 참조).
- **Docker 의존 테스트는 daemon 이 없으면 skip 됩니다.** 로컬 초록불이 컨테이너
  경로의 동작을 보장하지 않습니다 — CI job 을 확인해야 합니다.
- **A2A 와 메모리는 전 구간으로 검증되지 않습니다.** allowlist 와 auto-recall
  경로는 유닛에서 증명되며, 살아있는 컨테이너를 가로지르지는 않습니다. 특히
  auto-recall 의 순위는 실제 embedding provider 를 기다립니다 (위 항목) —
  `HashEmbedder` 로 돌리면 유닛이 이미 증명한 것을 되풀이할 뿐입니다.
- **재시작을 넘는 service iteration 은 검증되지 않았습니다.** idle backoff,
  iteration checkpoint, drain, `GRAPH_005` 는 fake clock 으로 증명됩니다.
  증명되지 **않은** 것은 구동 중인 컨테이너를 실제로 재시작한 뒤 service run 이
  올바르게 이어지는가입니다.
