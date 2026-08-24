# AI 작업 진행 규약

이 문서는 Claude / Copilot 등 AI 협업 도구가 본 프로젝트(Malkuth)에서 작업을 진행할 때
따라야 하는 **workflow 규약** 입니다. 작업 도중 사용자 승인을 빈번히 요청하여 흐름이 끊기는
문제를 해소하고, AI 의 자율성과 안전성의 균형을 명문화합니다.

본 규약은 IssueTracker 프로젝트에서 검증된 규약(이슈 #152, #199, #210, #212)을 이식한
것입니다. 코드 자체의 규약은 [01-architecture.md](01-architecture.md) ~
[07-code-style.md](07-code-style.md) 참조.

> **⚠️ 이식 상태**: 아래 규약이 참조하는 보조 도구는 IssueTracker 에서 아직 이식되지 않았다.
> 이식 전까지는 명시된 fallback 을 사용한다.
>
> | 도구 | 상태 | 이식 전 fallback |
> |---|---|---|
> | `scripts/gh-meta.sh` | 미이식 | `gh issue edit --add-label` / `gh api graphql` 직접 호출 |
> | `scripts/pr-resolve-comments.sh` | 미이식 | 개별 `gh api` 호출 |
> | `.github/PULL_REQUEST_TEMPLATE.md` | 미작성 | 규약 4 의 섹션 구성을 본문에 직접 작성 |
> | PR 타이틀 lint / Commit lint CI | 미구성 | 규약 6 표기를 수동 준수 |
> | Issue Type ID (본 repo) | 미조회 | 규약 6 의 조회 명령으로 최초 1회 조회 후 본 문서 갱신 |

<br>

## 핵심 6 규약

### 1. 이슈 먼저 (issue-first) 생성 정책

사용자 작업 지시가 도착하면 **코드 수정 시작 전에 GitHub 이슈를 먼저 생성** 한다.

#### 원칙

- 모든 작업은 GitHub 이슈로 추적 — branch / commit / PR 모두 그 이슈를 참조
- **이슈 생성 시 Label · Issue Type 부여 필수** (규약 6 매핑 표 참조)
- **규모가 PR 1개로 reviewable diff 안 되면 메인 이슈 + sub-issue N개로 분할** 하여 모두 사전에 생성
  - 메인 이슈 본문에 전체 그림 + sub-issue 목록 + 완료 조건 명시
  - **GitHub 의 Sub-issue 기능 + Relation 적극 활용** — `gh api graphql` 의 `addSubIssue`
    mutation 으로 메인 ↔ sub 관계 활성화하여 GitHub UI 에서 계층 명시
- 각 sub-issue 단위로 `branch → 작업 → commit → PR` 사이클 반복
- PR closing reference 는 그 sub-issue (`Closes #<sub>`). 메인 이슈는 모든 sub-issue 가
  close 될 때까지 OPEN 유지하고 마지막 sub-issue PR 에서 함께 close

#### Why

- 작업 진입 전 사용자와 scope 합의가 강제됨 → 작업 도중 방향 이탈 / scope creep 회피
- ad-hoc 으로 PR 직전에 이슈 만드는 패턴 차단
- 메인 이슈와 sub-issue 의 계층 관계가 GitHub 상에서 명확히 link

#### 예외

- 사용자가 명시적으로 **"이슈 없이 진행해"** / **"단발 hotfix"** 라고 지시한 경우
- 1줄 typo 수정 / 명백한 작은 chore
- **`gh` CLI 를 사용할 수 없는 환경** — 로컬 branch + commit 으로 진행하고, 이슈/PR 생성이
  불가했음을 작업 보고에 명시 (사용자가 후속 처리)

#### 판단 모호 시

규모가 작아 보이더라도 **이슈 1개 생성** — 작업 끝난 후 PR 본문 정리할 때 추가 비용 거의 없음.
"이거 큰가?" 가 50/50 이면 **메인 + sub-issue 분할 쪽** 으로 보수 분류.

#### Sub-issue 등록 명령

```bash
# 메인 이슈에 sub-issue 등록 (Relation 자동 활성화)
MAIN_ID=$(gh issue view <MAIN_NUMBER> --json id --jq .id)
SUB_ID=$(gh issue view <SUB_NUMBER> --json id --jq .id)

gh api graphql -f query='
mutation($issueId: ID!, $subIssueId: ID!) {
  addSubIssue(input: {issueId: $issueId, subIssueId: $subIssueId}) {
    issue { number }
    subIssue { number }
  }
}' -f issueId="$MAIN_ID" -f subIssueId="$SUB_ID"
```

<br>

### 2. 자율 진행 정책 — 승인 요청 최소화

쿼리의 의도가 명확하면 AI 는 **사용자 승인 없이 진행** 한다.

> **핵심 원칙 1**: 아래 "자율 진행 영역"에 해당하는 작업은 **절대 사용자 확인을 요청하지 않는다.**
> 스크립트 실행, 파일 수정, 커밋, 브랜치 생성 등은 묻지 않고 즉시 실행한다.
> 이미 허용된 도구(`Bash`, `Edit`, `Write`, `gh`, `git`, `make`, `uv`, `docker` 등)는 추가 승인
> 없이 사용한다.
>
> **핵심 원칙 2**: 최대한 자율적으로 진행하되, "예외 영역"에 해당하는 작업은 반드시 사용자
> 확인을 받는다.

#### 예외 영역 (반드시 사용자 확인)

| 영역 | 예시 |
|---|---|
| **시스템 자체 변경** | OS / 패키지 / 글로벌 환경 변경 (`apt-get install`, `sudo systemctl`, Docker daemon 설정 변경 등) |
| **언급 없는 destructive 권한** | `git push --force`, branch 삭제, DB `DROP TABLE`, `git reset --hard`, 무인 PR merge, `docker system prune -a`, 운영 볼륨/이미지 삭제 |
| **외부 시스템 영향** | PR merge / issue close / 배포 트리거 / 외부 API 비용 결제 (실 LLM 대량 호출 포함) / 외부 DM 발송 |
| **모호한 작업 범위** | 사용자 의도가 다중 해석 가능하거나 scope 가 불명확한 경우 — 진행 전 구체화 질문 |

#### 자율 진행 영역 (승인 불필요 — 즉시 실행)

- 코드 작성 / 수정 / 리팩토링 / 삭제
- 테스트 추가 / 갱신
- 새 파일 / 디렉토리 / 모듈(스킬셋·프롬프트셋·그래프) 생성
- 의존성 추가 (`uv add`) — 단 신규 외부 모듈은 규약 5 적용
- Branch 생성, 정상 push (force-push 아닌)
- Commit 단위 결정 및 실행
- PR 본문 작성
- **`scripts/` 하위 스크립트 실행** — 프로젝트 도구는 경로 확인 없이 실행
- **이슈 / PR Label · Type 부여** — 별도 승인 불필요
- **lint / fmt / typecheck / test / build** — `make` 또는 `uv run` 실행, 경로 확인 불필요
- **로컬 개발용 Docker 조작** — 테스트/개발 컨테이너의 build / run / stop / rm
  (운영 리소스 및 프레임워크 외부의 컨테이너는 예외 영역)

#### 판단 모호 시

"이게 destructive 영역인가?" 가 50/50 이면 **사용자 확인 쪽으로 보수 분류**. 단,
이미 자율 진행 영역으로 열거된 항목은 50/50 이 아니다 — 확인 없이 진행한다.

<br>

### 3. Commit-per-TODO 정책

별다른 사용자 언급이 없으면, AI 는 작업을 **논리적 변경 단위 (TODO)** 마다 commit 한다.

> **금지**: 작업을 모두 완료한 뒤 한 번에 몰아서 커밋하는 것은 **절대 허용하지 않는다.**
> 논리적 변경 단위가 완성되는 즉시 커밋한다. 커밋을 뒤로 미루지 않는다.

#### 원칙

- 논리적 변경 단위 완성 → **즉시 커밋** (다음 단위 작업 시작 전)
- 큰 PR 도 reviewable diff 단위로 분할 commit
- 각 commit 메시지는 [07-code-style.md](07-code-style.md) 의 컨벤션 준수:
  - **Prefix 는 다섯 가지 중 택 1**: `[FEAT]:` / `[FIX]:` / `[REFAC]:` / `[DOCS]:` / `[CHORE]:`
  - 이후 한국어 + 변경 의도
  - 단일 commit 이 너무 큰 변경을 담지 않도록
- 빌드 그린 유지 — 각 commit 이 lint + typecheck + unit test 통과 가능한 상태

#### 잘못된 패턴 (금지)

```text
# BAD — 작업 완료 후 모아서 한 번에 커밋
[파일 A 수정] → [파일 B 수정] → [파일 C 수정] → 커밋 하나로 묶음
```

#### 올바른 패턴

```text
# GOOD — 논리 단위마다 즉시 커밋
[파일 A 수정 — 기능 X 구현] → 커밋
[파일 B 수정 — 기능 Y 구현] → 커밋
[파일 C 수정 — 테스트 추가] → 커밋
```

#### 예외

- 사용자가 명시적으로 "한 번에 묶어줘" / "squash 해줘" 요청 시 단일 commit
- 사용자가 "단일 fix 만 해줘" 등 명백한 단일 변경을 지시한 경우

<br>

### 4. PR 자동 생성 정책

작업 완료 직후 (별다른 언급 없으면) AI 는 **PR 을 자동 생성** 한다.

#### 컨벤션 + 템플릿 준수

- **PR 타이틀**: `[카테고리#이슈번호] 제목` (규약 6 표기 체계)
- **본문**: `.github/PULL_REQUEST_TEMPLATE.md` 의 모든 섹션 채움 (템플릿 이식 전에는
  아래 섹션을 직접 구성)
  - 연관 이슈 (`Closes #N` — closing reference 명시)
  - 구현 내용
  - CI / 머지 게이트 점검
  - 변경 영향 범위 + 위험도
  - 롤백 계획
- **이슈 링크**: PR 본문 또는 Development sidebar 에 closing reference
- **Label 부여 필수**: 규약 6 매핑 표에 따라 PR 에도 동일 label 부여

#### 예외

- 작업이 이슈와 무관한 단발성 chore (예: 작은 hotfix, 운영 스크립트) — 사용자가
  "이슈 없이 PR 올려줘" 또는 "이슈 먼저 만들어줘" 명시
- 작업이 PR 단위가 아닌 운영 명령 (예: 그래프 배포, log 분석) 만 요청한 경우
- `gh` CLI 부재 환경 — 규약 1 예외와 동일하게 보고로 대체

<br>

### 5. 권한 사용 최소화

자율 진행 시, **꼭 필요한 경우가 아니면 이미 허용된 권한 범위 내에서만 동작** 한다.

#### 원칙

- 새 `Bash(...)` permission 요청은 작업 완수에 불가피한 경우에만
- 동등 효과를 낼 수 있는 기존 허용 도구가 있으면 그것을 우선 사용 — 새 외부 도구 설치 X,
  기존 `gh` / `uv` / `git` / `make` / `docker` 등 활용
- `WebFetch` / `WebSearch` 도 새 도메인은 작업 명시적 필요 시에만
- 신규 외부 의존성 (Python package / system package / Docker base image 변경) 추가는
  **규약 2 의 "모호 영역"** 으로 간주 → 사용자 사전 확인
- **실 LLM API 호출이 발생하는 실행** (수동 E2E, 실 모델 검증) 은 비용 발생 —
  외부 시스템 영향으로 간주하여 사전 확인

#### 이유

- 누적 권한이 늘어날수록 `.claude/settings.local.json` 정리 비용 증가
- 잘못된 도구 도입은 보안 노출 위험 증가 (토큰 노출, 시스템 파괴 명령)

<br>

### 6. 이슈 / PR 분류 메타데이터 정책 — Label · Issue Type

이슈 / PR 생성 시 **항상 Label 부여** (필수). 이슈는 추가로 **Issue Type 부여** (필수).

#### Prefix 표기 체계 (commit / PR / 이슈 — 3 분리 설계)

본 repo 는 commit / PR / 이슈 제목의 prefix 를 **의도적으로 다른 표기** 로 운용한다.

| 위치 | 형식 | 예시 | 강제 |
|---|---|---|---|
| Commit message | `[FEAT]:` / `[FIX]:` / `[REFAC]:` / `[DOCS]:` / `[CHORE]:` (축약 + 콜론) | `[FIX]: MCP 재연결 처리 보정` | (CI 이식 예정) |
| PR title | `[FEAT#N]` / `[FIX#N]` / `[REFAC#N]` / `[DOCS#N]` / `[CHORE#N]` (commit 카테고리 + #이슈번호, 콜론 없음) | `[DOCS#2] 룰셋 재작성` | (CI 이식 예정) |
| **Issue title** | **`[FEATURE]` / `[FIX]` / `[REFACTOR]` / `[DOCS]` / `[CHORE]` / `[HOTFIX]`** (full-word, 콜론 없음) | `[FEATURE] Docker 런타임 구현` | (규약 6 운영) |

**핵심 차이**: 이슈 prefix 는 commit prefix 의 축약형이 아니라 **원본 단어** 를 그대로 쓴다.
또한 commit/PR 에는 없는 `[HOTFIX]` 가 이슈 prefix 에만 존재 — 배포 중 긴급 대응이라는 별도
카테고리. 본 매핑 표는 **이슈 prefix → Label/Type** 매핑이다.

#### Label 매핑 (이슈 prefix 기준)

| Issue prefix | 기본 Label | 추가 Label (조건부) |
|---|---|---|
| `[FEATURE]` | `enhancement` | — |
| `[REFACTOR]` | `refactor` | — |
| `[CHORE]` | `chore` | — |
| `[DOCS]` | `documentation` | — |
| `[FIX]` (일반 에러 이슈) | `bug` | — |
| `[HOTFIX]` (배포 중 긴급) | `bug` | + `hotfix` |

PR 의 Label 은 그 PR 이 닫는 이슈 (`Closes #N`) 의 Label 과 동일하게 부여한다.

> `refactor`, `hotfix` 라벨은 GitHub 기본 라벨이 아니다 — 본 repo 최초 사용 시 생성 필요:
> `gh label create refactor --color D4C5F9`, `gh label create hotfix --color B60205`

#### Issue Type 매핑 (이슈 전용)

GitHub Issue Type 은 라벨과 별개의 native 분류 — `gh api graphql` 의
`updateIssueIssueType` mutation 으로 부여.

| Issue prefix | Issue Type |
|---|---|
| `[FEATURE]` | `Feature` |
| `[FIX]` / `[HOTFIX]` | `Bug` |
| `[REFACTOR]` / `[CHORE]` / `[DOCS]` | `Task` |

본 repo (EinSofINTEREST/Malkuth) 의 Issue Type ID 는 **아직 조회 전** — 최초 부여 시 아래
명령으로 조회하고 본 문서의 이 절을 갱신한다:

```bash
gh api graphql -f query='
query { repository(owner: "EinSofINTEREST", name: "Malkuth") {
  issueTypes(first: 10) { nodes { id name } }
} }'
```

#### 부여 명령 예시

> `scripts/gh-meta.sh` 이식 후에는 스크립트 사용이 표준 (이슈: Label + Type 동시 부여,
> PR: Label 부여). 이식 전에는 아래 직접 호출을 사용한다.

**이슈 생성 시**:
```bash
gh issue create --repo EinSofINTEREST/Malkuth \
  --title "[FEATURE] Docker 런타임 구현" \
  --body "..." \
  --label enhancement
# Issue Type 은 updateIssueIssueType mutation 으로 부여
```

**PR 생성 후**:
```bash
gh pr create --title "[FEAT#3] Docker 런타임 구현" --body "..."
gh pr edit <PR_NUMBER> --add-label enhancement
```

#### Why

- Label 누락 시 GitHub Issues / PR 필터링이 무력화 — `is:issue label:bug` 같은 운영 쿼리가
  불완전
- Issue Type 은 native 분류로, label 보다 강한 시멘틱 (Project Roadmap 의 Type 컬럼 자동 반영)
- `hotfix` 라벨은 우선순위 알림 / 라우팅 트리거에 활용 가능

#### How to apply

- **이슈 생성 직후** Label + Type 부여 — 까먹지 않도록 생성 직후 같은 회차에서 처리
- **PR 생성 직후** 닫는 이슈의 Label 과 동기화
- 매핑이 모호하면 가장 큰 변경 의도 prefix 기준으로 분류 + 보조 label 추가 가능

<br>

## 적용 흐름 (요약)

사용자 요청 도착 →
1. **의도가 명확한가?** Yes → 진행 / No → 구체화 질문 (규약 2 의 모호 영역)
2. **이슈 생성 + Label/Type 부여** (규약 1 + 규약 6) — 단발은 이슈 1개 / 큰 작업은 메인 +
   sub-issue N개로 분할 후 모두 사전 생성. "이슈 없이 진행해" 명시 또는 `gh` 부재 시 skip
3. **destructive / 시스템 / 외부 영향?** Yes → 사용자 확인 / No → 진행
4. **새 권한 / 외부 의존성 필요?** Yes → 사용자 확인 / No → 진행 (규약 5)
5. **작업 진행** — sub-issue 단위로 branch / 논리 단위마다 commit (규약 3)
6. **작업 완료 → PR 자동 생성 + Label 부여** (규약 4 + 규약 6) — `Closes #<sub-issue>` 명시,
   마지막 sub-issue PR 에서 메인 이슈도 close

<br>

## 참고 자료

- 규약 도입 배경 (원 프로젝트): IssueTracker 이슈 #152 (자율 진행), #199 (issue-first),
  #210 (Label·Type·Sub-issue), #212 (prefix 3분리)
- 관련 규약:
  - [07-code-style.md](07-code-style.md) — commit/PR 메시지 컨벤션
  - [06-testing.md](06-testing.md) — 작업 단위 테스트 기준
- 관련 문서: `.github/PULL_REQUEST_TEMPLATE.md` (이식 예정)
