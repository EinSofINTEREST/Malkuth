# Malkuth 문서

한국어 | **[English](../en/README.md)**

LangGraph 기반 모듈형 멀티 에이전트 오케스트레이션 프레임워크 **Malkuth** 의 사용자/운영자
문서입니다.

> 개발 룰셋의 원본은 [`.claude/rules/`](../../.claude/rules/README.md) 에 있습니다.
> 본 문서는 요약과 안내를 담당하며, 룰셋과 불일치할 경우 룰셋이 우선하고 문서를 수정해야
> 합니다.

## 목차

| 문서 | 내용 |
|---|---|
| [architecture.md](architecture.md) | 시스템 계층, 상호작용 모델, 실행 모드, 리소스 스코프 |
| [getting-started.md](getting-started.md) | 사전 요구사항, 환경 구성, 첫 솔루션 조립 |
| [modules.md](modules.md) | 모듈 시스템 — 스킬셋/프롬프트셋/메모리셋/그래프/그룹 |
| [testing.md](testing.md) | 테스트 전략, 결정성 규칙, 품질 게이트 |
| [ci/conventions.md](ci/conventions.md) | 저장소 거버넌스 및 CI 설계 규칙 |
| [ci/status-checks.md](ci/status-checks.md) | Required status check 이름의 단일 소스 |

## 요구사항

Python 3.12+ · [uv](https://docs.astral.sh/uv/) · Docker Engine 24+ (런타임/통합/E2E 경로에 필요)

```bash
uv sync --frozen        # 또는: make install
```

## 명령 레퍼런스

루트 [README](../../README.md#commands) 의 한국어 대응본입니다.

### 품질 게이트

```bash
make lint               # ruff check + ruff format --check
make typecheck          # mypy (malkuth.core 는 strict)
make test               # unit 테스트 + 커버리지 게이트 (>= 70%)
make check              # lint + typecheck + test — push 전에 이것을 돌린다
```

통합/E2E 는 Docker 가 필요하므로 기본 실행에서 빠져 있습니다:

```bash
make test-integration   # integration 마커 — Docker 컨테이너, 실제 MCP 세션
make test-e2e           # e2e 마커 — 전체 compose 스택 (CI 는 nightly)
```

Checkpointer 통합 테스트는 백엔드 주소가 없으면 skip 됩니다:

```bash
export MALKUTH_TEST_POSTGRES_URL=postgresql://malkuth:malkuth@127.0.0.1:15432/malkuth
export MALKUTH_TEST_REDIS_URL=redis://127.0.0.1:16379    # RediSearch 필요 (redis-stack)
```

### 이미지와 스택

```bash
make build              # malkuth/agent-base + agent-echo + agent-claude-code 이미지
make up                 # 개발 스택 — echo 에이전트 1대, control port 18080
make down
make e2e-up             # E2E 스택 — fake provider, memory service, 참조 에이전트 4대
make e2e-down
```

`make build` 를 먼저 돌리세요. compose 파일은 `malkuth/agent-base` 를 확장하지만 그것을
빌드하지는 않기 때문에, 다시 굽지 않고 스택을 올리면 **옛 이미지를 검증**하게 됩니다
([#222](https://github.com/EinSofINTEREST/Malkuth/issues/222)).

E2E 스택이 노출하는 포트: 에이전트 control **18081-18084**, Memory Service **18090**,
checkpoint Postgres **15433**, 에이전트 metrics **19082-19084**, A2A **19102-19104**.
에이전트는 fake 모델 provider 에 붙으므로 실제 LLM 으로 나가지 않습니다.

### CLI

```bash
uv run malkuth <command>          # venv 를 활성화했다면 그냥 `malkuth`
```

| 명령 | 하는 일 |
|---|---|
| `malkuth validate` | 저장소의 모든 그래프·매니페스트·모듈 ref 를 검증 |
| `malkuth deploy <graph.yaml>` | 그래프 하나를 배포 게이트로 검증 (`--a2a-port-range`) |
| `malkuth status` | 선언된 에이전트/그래프/그룹/모듈 요약 |
| `malkuth config [env]` | 해석된 설정 출력 (`dev` / `staging` / `prod`) |
| `malkuth check <state.yaml>` | 관측 상태와 대조해 정합성 불일치 보고 |
| `malkuth run <graph.yaml>` | mission 또는 service run 제출 |
| `malkuth run-list` / `run-drain <id>` / `run-resume <id>` | control plane 을 통한 run 조작 |

`--json` (서브커맨드 앞에) 은 기계 판독 출력으로, `--root` 는 작업 디렉토리가 아닌 다른
저장소를 가리킬 때 씁니다.

그래프 실행에는 각 에이전트 Control API 의 주소가 필요합니다 — 오케스트레이터는 에이전트가
어디 있는지 추측하지 않습니다. E2E 스택 기준:

```bash
export MALKUTH_AGENT_TOKEN=e2e-token

# mission run — END 에 도달하면 종료하고 최종 state 를 출력
uv run malkuth run graphs/research-pipeline.yaml \
  --input '{"query": "malkuth architecture"}' \
  --agent planner=http://127.0.0.1:18082 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent writer=http://127.0.0.1:18084

# service run — 상주 루프. 여기서는 종료되도록 횟수를 제한
uv run malkuth run graphs/feed-monitor.yaml --service --iterations 2 \
  --agent researcher=http://127.0.0.1:18083 \
  --agent planner=http://127.0.0.1:18082 \
  --agent writer=http://127.0.0.1:18084
```

`--iterations` 없는 service run 은 중단할 때까지 돕니다. `Ctrl-C` 는 drain 요청이므로
진행 중인 iteration 을 끝낸 뒤 정지합니다 — 중간에 끊기지 않습니다.

기대기 전에 알아야 할 한계가 둘 있습니다. `--checkpointer` 는 현재 `memory` 로만 동작합니다 —
CLI 가 `postgres`/`redis` 의 접속 URL 을 넘길 통로가 없습니다
([#220](https://github.com/EinSofINTEREST/Malkuth/issues/220)). 그동안 영속 checkpoint 는
라이브러리 API 로 접근할 수 있습니다. 그리고 `run-list` / `run-drain` / `run-resume` 는
아직 제공되지 않는 control plane 프로세스를 필요로 합니다
([#221](https://github.com/EinSofINTEREST/Malkuth/issues/221)).

### 상주 프로세스

```bash
python -m malkuth.agentd     # 컨테이너 내부 에이전트 데몬 — Control API 8080
python -m malkuth.memory     # Memory Service — HTTP 표면 + 비동기 인덱싱 루프
```

`agentd` 는 runtime layer 가 모든 에이전트 컨테이너 안에서 띄우는 프로세스입니다.
`MALKUTH_MANIFEST`, `MALKUTH_AGENT_TOKEN`, `MALKUTH_ROOT` 를 읽고, 메모리가 배선된
경우 `MALKUTH_MEMORY_URL` 과 `MALKUTH_MEMORY_TOKEN` 또는 `MALKUTH_MEMORY_TOKEN_FILE` 을
읽습니다.

Memory Service 는 `MALKUTH_REPO_ROOT`, `MALKUTH_MEMORY_PORT`,
`MALKUTH_MEMORY_TOKENS_PATH` 를 읽습니다. 별도 프로세스로 떠 있어야 합니다 — append 는
즉시 커밋되지만 인덱싱은 비동기라, 이 루프가 없으면 저장된 것이 검색되지 않습니다.

둘 다 `MALKUTH_LOG_LEVEL`, `MALKUTH_LOG_FORMAT`, `MALKUTH_METRICS_PORT` 를 따릅니다.

### 설정

설정은 `configs/{env}.yaml` (`dev`, `staging`, `prod`) 에 있습니다. CLI 는 위치 인자로
고르고, 상주 프로세스는 `MALKUTH_ENV` 를 읽습니다:

```bash
uv run malkuth config prod            # 해석된 prod 설정 출력
```

Override 는 **이중 언더스코어**로 섹션과 키를 구분합니다:

```bash
MALKUTH_ORCHESTRATOR__NODE_TIMEOUT_S=600 uv run malkuth config
```

언더스코어가 하나인 `MALKUTH_*` 는 설정이 아니라 프로세스 설정값입니다
(`MALKUTH_AGENT_TOKEN` 같은 것들). 로더는 이들을 **의도적으로 무시**합니다 — 컨테이너에
주입한 에이전트 env 가 그 컨테이너의 설정을 오염시키지 못하게 하기 위함입니다.

솔루션을 처음부터 조립하는 안내는 [getting-started.md](getting-started.md) 를 보세요.

## 언어 정책

- `docs/en/` 이 source of truth — 영어 먼저 작성
- `docs/ko/` 는 동일 구조의 한국어 번역 미러
- 두 버전은 항상 동기화 (`Docs Sync Check` 가 구조 미러를 강제)

## 추가 예정

- `runbooks/` — 운영 복구 절차 (런타임 구현과 함께 추가,
  [05-error-handling.md](../../.claude/rules/05-error-handling.md) 참조)
- `api.md` — Control Plane / Agent Control API 레퍼런스 (인터페이스 구현 이후)
