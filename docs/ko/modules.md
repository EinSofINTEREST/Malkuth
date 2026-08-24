# 모듈 시스템

한국어 | **[English](../en/modules.md)**

Malkuth 에서 배포 가능한 모든 것은 모듈입니다: 솔루션은 버저닝된 모듈의 배선으로
조립되며, 프레임워크 코드를 수정하지 않습니다. 규범 규칙:
[04-module-system.md](../../.claude/rules/04-module-system.md),
[09-memory-context.md](../../.claude/rules/09-memory-context.md).

## 모듈 타입

| 타입 | 선언 내용 | 위치 |
|---|---|---|
| **Skillset** | Python tool (능력) | `modules/skillsets/{name}/{version}/` |
| **Promptset** | Jinja2 템플릿 + 변수 스키마 | `modules/promptsets/{name}/{version}/` |
| **Memoryset** | 메모리 정책 — scope, 인덱스, 보존, recall | `modules/memorysets/{name}/{version}/` |
| **Agent** | 계약 — 모델, 모듈, 프로토콜, 리소스 | `agents/{name}/manifest.yaml` |
| **Graph** | Goal — nodes, edges, connections, mode | `graphs/{name}.yaml` |
| **Group** | 리소스 스코프 경계 — quota, secrets, 메모리 | `groups/{name}.yaml` |

## 참조 형식

```
{type}/{name}@{version}     예: skillsets/web-search@0.2.0
```

- 항상 정확한 semver — `latest`, 브랜치, 커밋 해시 금지
- 참조 해석은 registry 가 담당 — 경로 하드코딩 금지
- 게시된 버전 디렉토리는 불변 — 변경은 새 버전으로

## 스킬셋

Skill 은 async Python 함수이며, tool 스키마는 시그니처와 docstring 에서 자동 생성됩니다
— 수기 JSON schema 없음.

```python
@skill
async def search(ctx: SkillContext, query: str, max_results: int = 10) -> list[dict]:
    """웹 검색을 수행하고 상위 결과를 반환합니다."""
    ...
```

핵심 규칙: async-first, secrets/로깅은 `SkillContext` 로만 접근, timeout 은
`skillset.yaml` 에 선언, 실패는 예외로 (agentd boundary 에서 변환).

## 프롬프트셋

템플릿은 그래프 `node_id` (direct 요청은 `default`) 로 선택됩니다. 변수는 스키마로
선언되며, 미선언 변수 사용 시 조용히 빈 값이 되지 않고 렌더 실패(`MOD_004`) 합니다.
Locale 오버라이드는 `locales/{lang}/`. 프롬프트 문구 변경도 반드시 version bump.

## 메모리셋

Memoryset 은 memory space 의 정책을 고정합니다: scope (`run | local | group | global`),
임베딩 모델(버전 고정), 청킹, 하이브리드 검색 가중치, 보존/compaction, recall 기본값
(k, 최소 점수, 토큰 예산). 부착 위치는 scope 와 일치해야 합니다: manifest(local),
그래프(run), `groups/<name>.yaml`(group), `groups/global.yaml`(global).

## 그래프

그래프는 배선 모듈입니다: 에이전트 추가/분리는 YAML 변경만으로 완료됩니다.

- `mode: mission` — END 에서 종료; cycle 은 `max_iterations` 필수
- `mode: service` — 상주형; idle backoff 정책 필수, iteration 마다 checkpoint
- `connections` — A2A peer 호출 allowlist (방향 유의, peer 는 동등)
- 배포 시 검증이 dangling ref / 미도달 노드 / mode 위반을 차단

## 그룹

그룹은 멤버 에이전트의 리소스(quota, secrets, 그룹 메모리)를 스코프합니다.
배선에는 영향이 없습니다: 같은 그룹이라도 서로 호출하려면 `connections` 선언이
필요합니다. 리소스 해석은 **local > group > global**.

## 호환성과 버저닝

- 에이전트는 정확한 모듈 버전을 고정 참조; 배포 검증이 요구사항을 대조
  (예: skillset `requires.env` ⊆ agent `env_allowlist`)
- Breaking change 기준: tool 시그니처/변수 스키마 변경 → minor 이상;
  그래프 state schema 변경 → major; 임베딩 모델 변경 → version bump + 재인덱싱
