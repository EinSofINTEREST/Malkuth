# Module System Rules — Skillsets, Promptsets, Graph Modules

## Core Principle: Modules Are Independent Deliverables

스킬셋, 프롬프트셋, 그래프는 에이전트 코드와 **독립적으로 배포/교체 가능한 모듈**이다.

1. **분리**: 모듈은 프레임워크 코드(`src/`)와 에이전트 코드(`agents/*/src/`)에 포함되지 않고
   `modules/`, `graphs/` 에 별도 존재한다
2. **버전**: 모든 모듈은 semver 로 버전을 갖고, 참조는 항상 버전 고정 (`name@version`)
3. **교체 가능**: 같은 계약(변수 스키마 / tool 시그니처)을 만족하는 모듈끼리는
   에이전트 코드 수정 없이 교체 가능해야 한다
4. **선언적 참조**: 모듈 사용은 manifest / graph config 의 ref 선언으로만 —
   코드에서 모듈 경로 직접 import 금지

### Module Reference Format

```
{type}/{name}@{version}

예:
  skillsets/web-search@0.2.0
  promptsets/researcher@0.1.0
  agents/planner@0.1.0
  graphs/research-pipeline@1.0.0
```

- `latest`, 브랜치명, 커밋 해시 참조 금지 — semver 만
- Registry 가 ref 를 실제 경로로 해석 (`modules/skillsets/web-search/0.2.0/` 등)

## Skillset Modules

### Directory Specification

```
modules/skillsets/web-search/
└── 0.2.0/
    ├── skillset.yaml          # 스킬셋 선언 (필수)
    ├── skills/                # Python tool 구현
    │   ├── __init__.py
    │   ├── search.py
    │   └── fetch.py
    ├── requirements.txt       # 스킬셋 전용 의존성 (선택)
    └── README.md              # 사용법 (권장)
```

### skillset.yaml

```yaml
apiVersion: malkuth/v1
kind: Skillset
metadata:
  name: web-search
  version: 0.2.0
  description: 웹 검색 및 페이지 fetch 스킬

spec:
  skills:
    - name: search
      entrypoint: skills.search:search      # module:function
      description: 웹 검색을 수행하고 상위 결과를 반환
      timeout_s: 30
    - name: fetch_page
      entrypoint: skills.fetch:fetch_page
      description: URL 의 본문 텍스트를 추출
      timeout_s: 60

  requires:
    env: [SEARCH_API_KEY]        # 필요 env — 에이전트 manifest env_allowlist 와 대조 검증
    python: ">=3.12"
```

### Skill Implementation Rules

```python
# skills/search.py
from malkuth.core.skill import skill, SkillContext


@skill  # 데코레이터가 pydantic 시그니처 → tool schema 자동 변환
async def search(ctx: SkillContext, query: str, max_results: int = 10) -> list[dict]:
    """웹 검색을 수행하고 상위 결과를 반환합니다.

    Args:
        query: 검색 질의
        max_results: 최대 결과 개수
    """
    ...
```

1. **Signature = Schema**: tool 의 입력 스키마는 함수 시그니처 + type hint 에서 자동 생성 —
   수기 JSON schema 작성 금지. docstring 이 tool description 이 된다
2. **Async First**: 모든 skill 은 `async def` — blocking I/O 는 `asyncio.to_thread`
3. **SkillContext**: 로거, secrets, artifact 저장소 접근은 ctx 를 통해서만 —
   전역 상태 / 모듈 레벨 클라이언트 초기화 금지
4. **Timeout**: skillset.yaml 의 `timeout_s` 를 초과하면 agentd 가 강제 취소
5. **Errors**: 실패는 예외로 — agentd boundary 에서 `MalkuthError` 로 변환됨.
   skill 내부에서 에러를 삼키고 빈 결과 반환 금지
6. **No Cross-Skillset Imports**: 스킬셋 간 import 금지 — 공통 로직이 필요하면
   프레임워크 `pkg` 성격의 유틸로 승격하거나 별도 스킬셋 의존성으로 선언

### Loading Isolation

- 스킬셋 코드는 **소유 에이전트의 컨테이너 안에서만** import/실행된다
- `requirements.txt` 는 에이전트 이미지 빌드 시 설치 — 런타임 pip install 금지
- 두 스킬셋의 의존성이 충돌하면 같은 에이전트에 조합 불가 — 배포 검증에서 차단

## Promptset Modules

### Directory Specification

```
modules/promptsets/researcher/
└── 0.1.0/
    ├── promptset.yaml         # 프롬프트셋 선언 (필수)
    ├── templates/
    │   ├── system.j2          # 시스템 프롬프트
    │   ├── research.j2        # 노드/태스크별 템플릿
    │   └── summarize.j2
    └── locales/               # 다국어 오버라이드 (선택)
        └── ko/
            └── system.j2
```

### promptset.yaml

```yaml
apiVersion: malkuth/v1
kind: Promptset
metadata:
  name: researcher
  version: 0.1.0
  description: 리서치 에이전트 프롬프트

spec:
  engine: jinja2
  default_locale: en
  templates:
    system:
      file: templates/system.j2
    research:
      file: templates/research.j2
      variables:                 # 변수 스키마 — 렌더 시 검증
        query: {type: string, required: true}
        depth: {type: integer, default: 2}
    summarize:
      file: templates/summarize.j2
      variables:
        documents: {type: array, required: true}
```

### Promptset Rules

1. **Variables Schema**: 템플릿 변수는 promptset.yaml 에 선언 — 미선언 변수 사용 시
   렌더 단계에서 `MOD_004` 에러 (silent empty rendering 금지)
2. **Engine**: Jinja2 고정, `autoescape` 비활성 (프롬프트는 HTML 이 아님),
   단 untrusted 입력 변수는 렌더 전 sanitize (지시문 주입 경계 표시)
3. **Locale**: `locales/{lang}/` 가 동일 파일명으로 오버라이드 —
   에이전트 manifest 또는 태스크 config 의 locale 로 선택
4. **No Logic in Templates**: 템플릿 안의 복잡한 분기/루프 지양 — 로직은 skill/agent 코드로,
   템플릿은 표현만
5. **Prompt Changes Are Versioned**: 프롬프트 문구 수정도 반드시 version bump —
   실험/롤백 추적 가능해야 함

## Graph Modules — Modular Agent Wiring

그래프는 에이전트를 잇고 분리하는 **배선 모듈**이다. 에이전트 연결 변경은 그래프 파일
수정만으로 완료되어야 한다.

### Graph Topology Specification

```yaml
# graphs/research-pipeline.yaml
apiVersion: malkuth/v1
kind: Graph
metadata:
  name: research-pipeline
  version: 1.0.0
  description: 질의 → 계획 → 리서치 → 작성 파이프라인

spec:
  state:
    schema: malkuth.graphs.schemas:ResearchState   # pydantic 모델 ref
    checkpointer: default                          # 프레임워크 설정 상속

  nodes:
    - id: planner
      agent: agents/planner@0.1.0
      input_map:                  # state → TaskRequest.input 매핑
        query: state.query
      output_map:                 # TaskResult.output → state 병합 매핑
        plan: output.plan

    - id: researcher
      agent: agents/researcher@0.1.0
      input_map: {plan: state.plan}
      output_map: {findings: output.findings}

    - id: writer
      agent: agents/writer@0.1.0
      input_map: {findings: state.findings}
      output_map: {report: output.report}

  edges:
    - {from: START, to: planner}
    - from: planner
      to: researcher
      condition: malkuth.graphs.conditions:needs_research   # 조건 함수 ref
    - {from: planner, to: END, condition: malkuth.graphs.conditions:plan_only}
    - {from: researcher, to: writer}
    - {from: writer, to: END}

  connections:                    # A2A 직접 호출 allowlist — 03 참조
    - {caller: researcher, callee: planner}
```

### Graph Rules

1. **Config Over Code**
   - 노드 추가/제거, edge 연결/분리는 그래프 YAML 수정만으로 완료
   - 그래프별 Python 코드는 state schema 와 조건 함수뿐 — 배선 로직 코드 작성 금지
2. **Validation** (배포 시, 실패하면 배포 중단)
   - 모든 `agent` ref 해석 가능
   - dangling edge 없음 (from/to 가 노드 or START/END)
   - START 에서 모든 노드 도달 가능, END 도달 가능
   - `input_map` 의 state 키가 state schema 에 존재
   - conditional edge 의 조건 함수 import 가능
   - `connections` 의 caller/callee 가 모두 그래프 노드
3. **Cycle Policy**: 순환 edge 는 허용하되 (self-loop 포함, 재시도/refinement 패턴)
   `max_iterations` 명시 필수 — 미명시 시 검증 실패
4. **State Schema**
   - pydantic 모델로 정의, 노드 산출물 병합은 `output_map` 으로만
   - 노드가 state 전체를 덮어쓰는 패턴 금지 — 선언된 키만 병합
5. **Subgraphs**: 그래프는 다른 그래프를 노드로 참조 가능
   (`graph: graphs/sub-review@1.0.0`) — 순환 참조는 검증에서 차단

### Attach / Detach Semantics

| 작업 | 방법 | 재배포 범위 |
|---|---|---|
| 에이전트를 그래프에 추가 | nodes + edges 에 항목 추가 | 그래프 리로드 (기존 run 은 기존 버전으로 완주) |
| 에이전트를 그래프에서 분리 | nodes/edges/connections 에서 제거 | 그래프 리로드. 참조가 0이 된 에이전트 컨테이너는 drain 후 정리 |
| 에이전트 버전 교체 | `agent:` ref 의 버전만 변경 | 새 버전 컨테이너 기동 → health OK → 트래픽 전환 → 구버전 drain |
| 연결(A2A) 추가/제거 | connections 수정 | 그래프 리로드 (edge token 재발급) |

- 실행 중인 run 은 시작 시점의 그래프 버전으로 완주한다 (mid-run 토폴로지 변경 금지)
- 그래프 버전도 semver — 토폴로지 변경 시 bump

## Module Registry

### v0.1: Filesystem Registry

```
modules/
├── skillsets/{name}/{version}/skillset.yaml
├── promptsets/{name}/{version}/promptset.yaml
agents/{name}/manifest.yaml            # 버전은 manifest 내부 선언
graphs/{name}.yaml
```

1. **Resolution**: `registry.resolve(ref)` 가 유일한 해석 경로 — 경로 하드코딩 금지
2. **Immutability**: 게시된 버전 디렉토리는 수정 금지 — 변경은 새 버전으로
3. **Integrity**: resolve 시 kind/name/version 이 ref 와 일치하는지 검증

### Compatibility Rules

1. 에이전트 manifest 는 모듈의 **정확한 버전**을 참조 (범위 지정 없음, v0.1 단순화)
2. Skillset 의 `requires.env` ⊆ agent manifest 의 `env_allowlist` — 배포 검증
3. Promptset 템플릿 이름 ⊇ 그래프에서 해당 에이전트가 사용하는 node_id 집합
   (agentd 가 `task.node_id` 로 템플릿을 선택하기 때문)
4. Breaking change 기준:
   - Skillset: tool 시그니처/이름 변경 = minor 이상
   - Promptset: 변수 스키마 변경 = minor 이상
   - Graph: state schema 변경 = major

## Testing Modules

모듈별 최소 테스트 기준 (상세는 [06-testing.md](06-testing.md)):

- **Skillset**: skill 단위 유닛 테스트 + schema 생성 스냅샷 테스트
- **Promptset**: 변수 스키마 검증 테스트 + 렌더링 골든 테스트 (스냅샷)
- **Graph**: 토폴로지 검증 테스트 + fake agent 로 라우팅 시나리오 테스트
