# 시작하기

한국어 | **[English](../en/getting-started.md)**

> **부트스트랩 안내**: Malkuth 는 v0.1.0 이전 부트스트랩 단계입니다 — 룰셋과 문서는
> 갖춰졌지만 프레임워크 코드는 아직 없습니다. *(planned)* 표시는 구현이 충족할 계약을
> 설명합니다.

## 사전 요구사항

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) (패키지 매니저 — lockfile 커밋)
- Docker Engine **24+** (에이전트 격리 런타임)
- `make` (작업 자동화)

## 환경 구성

```bash
git clone https://github.com/EinSofINTEREST/Malkuth.git
cd Malkuth
uv sync                 # (planned) 고정된 의존성 설치
```

로컬/CI 공통 품질 게이트:

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (src/malkuth/core 는 strict)
make test               # pytest unit + 커버리지 게이트 (≥ 70%)
make test-integration   # Docker 기반 통합 테스트
```

## 첫 솔루션 조립

솔루션은 **모듈 조립**으로 구성합니다 — 프레임워크 코드를 작성하는 것이 아니라 기존
에이전트와 모듈을 그래프로 배선합니다.

### 1. 에이전트 선언

```yaml
# agents/researcher/manifest.yaml
apiVersion: malkuth/v1
kind: Agent
metadata:
  name: researcher
  version: 0.1.0
  group: research               # 리소스 스코프 소속 (선택)
spec:
  model: {provider: anthropic, name: claude-sonnet-5}
  promptset: {ref: promptsets/researcher@0.1.0}
  skillsets:
    - ref: skillsets/web-search@0.2.0
  memory:
    spaces:
      - {ref: memorysets/agent-longterm@0.1.0, as: longterm}
  a2a: {enabled: true}
  runtime:
    resources: {cpu: "1.0", memory: 1Gi}
    env_allowlist: [ANTHROPIC_API_KEY]
```

### 2. 그래프 배선 (goal)

```yaml
# graphs/research-pipeline.yaml
apiVersion: malkuth/v1
kind: Graph
metadata: {name: research-pipeline, version: 1.0.0}
spec:
  mode: mission                 # mission(달성형) | service(상주형)
  goal: 질의를 받아 리서치 보고서를 완성한다
  nodes:
    - {id: planner, agent: agents/planner@0.1.0}
    - {id: researcher, agent: agents/researcher@0.1.0}
    - {id: writer, agent: agents/writer@0.1.0}
  edges:
    - {from: START, to: planner}
    - {from: planner, to: researcher}
    - {from: researcher, to: writer}
    - {from: writer, to: END}
  connections:                  # A2A peer 호출 allowlist (동등한 peer)
    - {caller: researcher, callee: planner}
```

### 3. 그룹 리소스 선언 (선택)

```yaml
# groups/research.yaml
apiVersion: malkuth/v1
kind: Group
metadata: {name: research}
spec:
  quotas: {cpu: "8.0", memory: 16Gi, max_agents: 10}
  secrets: [SEARCH_API_KEY]
  memory:
    spaces:
      - {ref: memorysets/domain-knowledge@0.1.0, as: knowledge, mode: rw}
```

### 4. 배포와 실행 *(planned)*

```bash
malkuth deploy graphs/research-pipeline.yaml   # 계약 검증 → 컨테이너 기동
malkuth run research-pipeline --input '{"query": "..."}'
malkuth status                                  # 에이전트 healthy 확인
malkuth run trace <run_id>                      # node 실행 타임라인
```

그래프 run 없이도 어느 에이전트든 직접 호출할 수 있습니다:

```bash
malkuth agent invoke researcher --input '{"query": "..."}'   # (planned)
```

## 다음 단계

- [architecture.md](architecture.md) — 구성 요소의 전체 그림
- [modules.md](modules.md) — 스킬셋/프롬프트셋/메모리셋 제작
- [testing.md](testing.md) — LLM 주변의 결정적 테스트 작성
- [.claude/rules/](../../.claude/rules/README.md) — 전체 개발 룰셋
