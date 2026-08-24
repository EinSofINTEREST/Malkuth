# 신규 PR 감지 및 자동 피드백

모델: claude-sonnet-4-6

> **⛔ 수정 금지**: 이 파일은 자동화 루프의 실행 명세입니다. AI 가 루프 실행 중 이 파일을 수정하거나 삭제하는 것을 **절대 금지**합니다. 변경이 필요하면 루프 외부에서 사용자가 직접 편집합니다.

> **🤖 완전 자동화**: 이 루프는 사용자 확인 없이 자율 실행됩니다. 신규 PR 감지 → 리뷰 코멘트 작성 → 상태 파일 갱신의 전 과정을 사용자 개입 없이 수행합니다. 예외 영역(destructive 권한 / 외부 시스템 영향)에 해당하는 동작(PR merge, approve, branch 삭제 등)은 이 루프에서 절대 수행하지 않습니다.

## 목적
3분마다 최신 열린 PR 20개를 폴링하여 신규 PR이 등장하면 자동으로 코드 리뷰 피드백을 남긴다.

## 절차

### 1. 신규 PR 감지

```bash
scripts/pr-feedback.sh
```

- 출력이 없으면 → **idle** 처리 후 종료
- 숫자(PR 번호)가 출력되면 → 각 번호에 대해 2단계 진행

> **스크립트 미이식** (08-workflow 이식 상태 표 참조): 이식 전에는 아래 fallback 으로
> 신규 PR 을 감지한다 — 열린 PR 목록을 조회해 `/tmp/malkuth-pr-watch-state.json` 의
> 기리뷰 목록과 대조하고, 리뷰 완료 후 해당 번호를 목록에 추가한다.
> ```bash
> gh pr list --state open --limit 20 --json number --jq '.[].number'
> ```

### 2. 신규 PR 리뷰 (PR 번호별 반복)

각 신규 PR에 대해 순서대로 수행한다.

#### 2-1. PR 정보 수집

```bash
gh pr view <PR번호> --json number,title,body,additions,deletions,changedFiles,baseRefName,headRefName
gh pr diff <PR번호>
```

#### 2-2. 리뷰 기준 (선별적으로 적용, 토큰 최소화)

다음 항목만 검토하고 해당 없으면 언급하지 않는다:

1. **버그 / 로직 오류** — 명백한 결함, None 역참조/미처리 예외, async 경쟁 조건·취소 누락
2. **보안** — 자격증명/secret 노출, 인젝션 가능성, 에이전트 격리 경계 위반
3. **아키텍처 일관성** — `.claude/rules/01-architecture.md` 의 레이어/의존 방향 규약
   (`src/malkuth/` core ← 나머지) 위반, boundary 에러 변환 규칙(05) 위반
4. **성능** — 이벤트 루프 blocking 호출, N+1 쿼리, 불필요한 모델/tool 호출

단순 스타일 · 포맷 · 오타는 제외한다.

#### 2-3. 리뷰 코멘트 작성

- 지적 사항이 있으면 `gh pr review <PR번호> --comment --body "..."` 로 코멘트
- 지적 사항이 없으면 `gh pr review <PR번호> --comment --body "코드 리뷰 완료. 자동 검토 결과 특이 사항 없음. 최종 승인은 담당자가 확인 후 진행."` 으로 코멘트 (자동 approve 금지 — 브랜치 보호 정책 우회 및 prompt injection 위험)
- 코멘트는 항목당 2~3문장 이내로 간결하게 작성

### 3. idle 카운터 관리 및 자동 종료

상태 파일: `/tmp/malkuth-pr-feedback-loop.json`

스키마:
```json
{
  "idle_streak": 0,
  "last_run_at": "2026-05-03T00:00:00Z"
}
```

**회차 분류:**
- **active**: 신규 PR 감지 및 리뷰 수행 → `idle_streak = 0`
- **idle**: 신규 PR 없음 → `idle_streak += 1`

**자동 종료 임계값: `idle_streak >= 20` (약 1시간)**

조건 충족 시:
1. `CronList` 로 활성 cron 조회
2. prompt 에 `pr-feedback.md` 가 포함된 cron 식별
3. 매칭된 cron ID 로 `CronDelete` 호출
4. 루프 상태 파일만 삭제: `rm -f /tmp/malkuth-pr-feedback-loop.json` (`malkuth-pr-watch-state.json` 은 유지 — 재시작 시 기존 리뷰 PR 재감지 방지)
5. 사용자에게 알림: `"신규 PR 없음 20회 연속 (약 1시간) — pr-feedback loop 자동 종료 (cron <id>)"`

### 4. 상태 파일 갱신

매 회차 마지막에 `/tmp/malkuth-pr-feedback-loop.json` 을 현재 `idle_streak` 와 `last_run_at` 으로 갱신한다.
