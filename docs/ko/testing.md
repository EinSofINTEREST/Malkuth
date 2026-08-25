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
