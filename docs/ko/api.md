# Control Plane API

한국어 | **[English](../en/api.md)**

Control Plane 은 에이전트 컨테이너 **바깥의 모든 것**을 소유하는 단일 프로세스다 — 선언
카탈로그, 그것을 쓰는 저작 표면, 그래프를 실제 컨테이너로 만드는 배포, 그 컨테이너를
구동하는 run. [Web UI](ui.md) 는 이 API 의 클라이언트일 뿐이며, 모든 화면은 아래 호출 중
하나다.

```bash
python -m malkuth.orchestrator
```

`configs/{MALKUTH_ENV}.yaml` 을 읽는다. 어떤 표면이 열리는지는 그 파일이 정한다:

| 표면 | 열리는 조건 |
|---|---|
| Run 조회·drain | 항상 |
| 카탈로그·저작 | 항상 — `registry.roots` 는 `MALKUTH_REPO_ROOT` 기준으로 해석된다 |
| 배포·run 제출·재개 | `orchestrator.deployment_store` 가 설정된 경우 |
| 빌드 재료 | `orchestrator.material_store` 가 설정된 경우 — 아니면 라우트가 `400` (`CFG_001`) 로 답한다 |
| 이미지 빌드, 커스텀 에이전트의 배포 게이트 | `orchestrator.material_store` **와** `orchestrator.build_store` 가 모두 설정된 경우 — 아니면 이미지 라우트가 없다 (`404`) |
| `/ui` 의 Web UI | 항상 |

`orchestrator.deployment_store` 가 없으면 컨테이너를 띄우지 않고
`POST /v1/runs/{id}/resume` 에 `501` 로 답한다 — run 기록을 읽기만 하고 구동하지는 않기
때문이다.

## 인증

**인증은 `orchestrator.control_token` 을 설정했을 때만 켜진다.** 토큰이 있으면 모든
`/v1/*` 라우트가 그것을 bearer 토큰으로 요구한다:

```bash
curl -H "Authorization: Bearer $MALKUTH_CONTROL_TOKEN" http://127.0.0.1:8700/v1/graphs
```

**토큰이 없으면 검사가 꺼지고 모든 라우트가 열린다.** 이 구성은 loopback 바인드에서만
허용된다 — 다른 주소에 토큰 없이 바인드하면 기동을 거부하고 (`CFG_001`), 토큰 없이 뜰
때는 경고를 남긴다. 운영자 한 명의 기계가 아니라면 토큰을 설정한다.

토큰을 설정해도 무인증인 라우트가 둘 있다: `GET /v1/health` (Docker healthcheck 가 직접
부른다) 와 `/ui` 의 정적 파일 (화면 자체는 데이터를 갖고 있지 않다).

토큰이 없거나 틀리면 `401` 이며, 이 응답은 아래의 에러 봉투가 **아니다**. 검사가 라우트보다
먼저 도는 의존성이라 FastAPI 자체의 `{"detail": "invalid control plane token"}` 과
`WWW-Authenticate` 헤더로 나간다.

## 에러

control plane 이 내는 실패는 같은 봉투를 쓰고, 안정적인 부분은 `code` 다 — 메시지가 아니라
코드로 분기한다. 두 응답만 이 봉투가 **아니다**: 위의 `401`, 그리고 run 을 구동하지 않는
control plane 의 `501` (더 납작한 레거시 모양).

```json
{
  "error": {
    "category": "not_found",
    "code": "NF_001",
    "message": "unknown graph: no-such-graph",
    "agent": null,
    "task_id": null,
    "retryable": false,
    "details": {"kind": "graph", "name": "no-such-graph"}
  }
}
```

상태 코드는 한 가지 기준을 따른다 — **누가 고칠 수 있는가**:

| 상태 | 의미 | 대표 코드 |
|---|---|---|
| `400` | 요청 형식이 틀렸거나 선언이 유효하지 않다 | `VAL_001`, `VAL_002`, `MOD_002`, `CFG_002` |
| `401` | 토큰 누락/불일치 | — |
| `404` | 그런 리소스가 없다 | `NF_001` |
| `409` | 현재 상태와 충돌하며, 호출자가 해소할 수 있다 | `GRAPH_006`, `RT_010`, `RT_011`, `RT_012` |
| `500` | 서버가 고쳐야 한다 | `RT_001`, `GRAPH_002`, `STOR_003` |
| `503` | 지금은 안 되지만 다시 걸어볼 만하다 | `retryable: true` 인 모든 것 |

`details` 에는 조치에 필요한 값이 담긴다 — 어느 필드가 틀렸는지, 어떤 버전을 기대했는지,
어떤 에이전트가 이미 배포돼 있는지.

## Health 와 UI

### `GET /v1/health`

무인증 liveness. `{"status": "ok"}`.

### `GET /` → `GET /ui/`

`/` 는 운영자 UI 로 리다이렉트한다. [UI 가이드](ui.md) 참조.

## 카탈로그

디스크에 있는 선언의 읽기 전용 뷰. 모든 목록은 배열 두 개를 돌려준다 — 파싱된 것은
`items`, 실패한 것은 `problems`. **하나가 깨져도 나머지를 가리지 않는다** — 그러지 않으면
매니페스트 하나가 깨졌을 때 카탈로그 전체가 `500` 이 되고, 어느 파일인지 알 수 없다.

### `GET /v1/agents`, `GET /v1/graphs`, `GET /v1/groups`

```json
{
  "items": [
    {
      "name": "planner",
      "version": "0.4.0",
      "group": "research",
      "description": "질의를 실행 가능한 리서치 계획으로 나누는 에이전트",
      "model": {"provider": "anthropic", "name": "claude-sonnet-5"}
    }
  ],
  "problems": []
}
```

그래프 요약은 `model` 대신 `mode`, `goal`, `nodes` 를 싣고, 그룹 요약은 `quotas` 를 싣는다.
`problem` 은 `path` 와 `code`, 메시지를 담는다. 선언(에이전트·그래프·그룹)은 두 종류의
실패 — 스키마 위반과 이름·위치 불일치 — 를 모두 `VAL_002` 로 보고한다. 모듈 목록은
레지스트리가 낸 코드를 그대로 싣는다 (`MOD_001`, `MOD_003`).

### `GET /v1/agents/{name}`, `GET /v1/graphs/{name}`, `GET /v1/groups/{name}`

파싱된 선언 전문 — 파일 텍스트가 아니라 런타임이 실제로 사용할 문서다. 이름이 없으면
`404` (`NF_001`).

### `GET /v1/modules/{module_type}`

`module_type` 은 `skillsets`, `promptsets`, `memorysets` 중 하나. 게시된 모든 버전을
나열한다:

```json
{"items": [{"name": "planner", "versions": ["0.1.0", "0.2.0", "0.3.0"]}], "problems": []}
```

### `GET /v1/modules/{module_type}/{name}/{version}`

모듈 문서 하나. 버전은 정확히 지정한다 — `latest` 는 없다.

## 저작

검증을 통과한 선언을 디스크에 쓴다. 위치가 곧 식별자다: `docs-demo` 라는 그래프는
`graphs/docs-demo.yaml` 에 있으므로 경로 세그먼트와 선언된 이름이 일치해야 한다 (아니면
`VAL_002`).

### `POST /v1/validate`

**아무것도 쓰지 않고** 초안을 검증한다. 편집기가 변경마다 부르는 것이 이것이다.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graphs": [ ... ], "agents": [ ... ]}' http://127.0.0.1:8700/v1/validate
```

```json
{"ok": false, "findings": [
  {"check": "mode_rules", "code": "GRAPH_001",
   "message": "mission graph must be able to reach END", "graph": "docs-demo"}
]}
```

finding 은 에러 상태가 아니라 `200` 이다 — "아직 유효한가?" 에 대한 정상적인 답이기
때문이다. 초안 형식 자체가 아니면 `400` (`VAL_002`).

### `PUT /v1/graphs/{name}`, `PUT /v1/agents/{name}`

선언 하나를 검증 후 저장한다. `{"name": ..., "path": ...}` 를 돌려준다.

편집기가 기대는 두 가지 규칙:

- **내용을 바꾸면 버전을 올려야 한다.** 같은 버전으로 다른 내용을 저장하면 `400`
  (`MOD_002`, `details` 에 `existing` 과 `proposed`). 같은 내용이면 멱등이다.
- **사용 중인 선언은 덮어쓰지 못한다.** 실행 중 run 이나 살아 있는 배포가 참조하면 거절된다
  (`VAL_002`, "agent is currently deployed — tear the deployment down first"). 배포를 먼저
  해체한다.

쓰기는 원자적이다 — 같은 디렉토리의 임시 파일에 쓴 뒤 rename 하므로, 쓰는 도중 죽어도
잘린 선언이 남지 않는다.

### `PUT /v1/declarations`

여러 선언을 **한 단위로** 저장한다 — 함께 검증하고, 하나라도 실패하면 함께 되돌린다.
노드와 그 노드가 가리키는 에이전트를 같이 추가할 때처럼 변경이 파일을 넘나들 때 쓴다:

```json
{"graphs": {"docs-bundle": { ... }}, "agents": {"echo": { ... }}}
```

```json
{"written": ["/repo/graphs/docs-bundle.yaml", "/repo/agents/echo/manifest.yaml"]}
```

### `DELETE /v1/graphs/{name}`, `DELETE /v1/agents/{name}`

성공 시 `204`. 참조가 남아 있으면 거절하며 (`400`, `VAL_002`), 사유에 따라 `details` 가
다르다 — 저장된 그래프가 쓰는 에이전트는 그 목록을 `referenced_by` 에 싣고, 현재 배포 중인
것은 `kind` 와 `name` 을 싣는다.

**선언만 지운다.** 매니페스트 파일은 사라지지만 `agents/` 아래의 디렉토리는 남고, 재료
스토어의 빌드 재료도 남는다. 재료는 버전에 묶여 있다: 같은 이름과 버전을 다시 선언하면 그
재료가 돌아오고, 그 버전에 다른 재료를 넣는 것은 여전히 거절된다 (`MOD_002`). 새로
시작하려면 버전을 올린다.

## 빌드 재료와 이미지

커스텀 에이전트는 **빌드 재료**를 가진 에이전트다: `Dockerfile`(선택)과 `src/` 트리. 재료는
저장소가 아니라 control plane 의 재료 스토어에 에이전트 이름과 선언된 버전을 키로 산다.
declarative 에이전트는 재료가 없고 굽지도 않는다 — base 이미지에 선언을 마운트해 돈다.

굽기는 명시적 단계다. 재료를 저장해도 굽지 않고, 배포도 굽지 않는다 — 그 버전의 이미지가
`built` 가 아닌 커스텀 에이전트는 배포가 **거절한다** (`409`, `RT_012`.
[`POST /v1/deployments`](#post-v1deployments) 참조).

### `GET /v1/agents/{name}/materials`

에이전트 **현재** 버전의 재료. 없는 것은 오류가 아니다:

```json
{"agent": "docs-custom", "version": "0.1.0", "files": {}, "updated_at": ""}
```

에이전트가 선언되지 않았으면 `404` (`NF_001`).

### `PUT /v1/agents/{name}/materials`

에이전트 현재 버전의 재료를 저장한다. 본문은 컨텍스트 상대 경로 → 텍스트 내용이다:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"files": {"src/marker.py": "MARK = 1\n"}}' \
  http://127.0.0.1:8700/v1/agents/docs-custom/materials
```

응답은 `GET` 과 같은 모양이고 `updated_at` 이 찍힌다. 규칙:

- **경로**는 `Dockerfile` 이거나 `src/` 아래, 상대·posix 형식, 이미 정규화된 형태여야 한다
  (`src/./a.py`, `src//a.py`, `..` 가 들어간 것은 거절). 파일은 최대 200개, 각각 텍스트
  256 KiB 까지. 위반은 `400` (`VAL_002`) 이고 `details` 에 문제의 `path` 가 실린다.
- **한 버전의 재료는 불변이다.** 같은 버전에 다른 내용은 `400` (`MOD_002`), 같은 내용은
  멱등이다. 에이전트 버전을 올려 에이전트를 저장한 뒤 새 재료를 저장한다.
- **배포 중인 에이전트의 재료는 바꿀 수 없다** (`400`, `VAL_002`).

`Dockerfile` 은 저장할 때가 아니라 구울 때 검사한다.

### `DELETE /v1/agents/{name}/materials`

`204`. 현재 버전의 재료를 비워 에이전트가 다시 declarative 에이전트로 돈다. 버전은 계속
점유된다: 그 뒤에 그 버전으로 다른 재료를 저장해도 `MOD_002` 다. 배포 중이면 거절한다.

### `POST /v1/agents/{name}/image`

빌드를 제출하고 곧바로 `202` 로 돌아온다:

```json
{"agent": "docs-custom", "version": "0.1.0", "status": "building",
 "image": "malkuth/agent-docs-custom:0.1.0", "error": null, "log": "",
 "updated_at": "2026-09-14T11:02:03.123456+00:00"}
```

빌더는 카탈로그와 스토어에서 임시 빌드 컨텍스트를 조립해 `malkuth/agent-<name>:<version>`
으로 굽고, 그 디렉토리를 지운다:

```
Dockerfile        # 사용자의 것, 저장하지 않았으면 스켈레톤
manifest.yaml     # 에이전트의 선언
modules/          # 모듈 루트
src/              # 재료
```

스켈레톤은 이 셋을 `/app` 에 복사하고 `/app/src` 를 `PYTHONPATH` 에 올린다. 그래서
매니페스트의 `spec.entrypoint: agent:MyAgent` 는 `src/agent.py` 로 해석된다. 직접 쓴
`Dockerfile` 은 다음을 지켜야 한다:

- `FROM malkuth/agent-base:<tag>` 로 시작한다 — base 에 `agentd` 가 들어 있다
- `root` 로 끝나지 않는다 (설치하려고 올라갔다 내려오는 것은 괜찮다)
- `COPY`/`ADD` 는 컨텍스트 안에서만 — 절대 경로, `..`, 원격 URL 금지

굽기 전에 거절되는 경우:

- `404` (`NF_001`) — 에이전트가 선언되지 않았다.
- `400` (`VAL_002`) — 재료가 없거나 `Dockerfile` 이 위 규칙을 어겼다.
- `409` (`RT_011`) — 이 버전의 빌드가 이미 진행 중이다. 두 빌드가 같은 태그를 쓰면 늦게
  끝난 쪽이 이긴다.

시작한 뒤 실패한 빌드는 HTTP 오류가 **아니다**. 아래 기록에 `failed` 로 남는다. `error` 는
어느 이미지가 실패했는지만 말하고, 원인은 Docker 출력의 꼬리인 `log` 에 있다:

```
Step 2/2 : RUN exit 3
 ---> Running in 80c5a8ac55e7
The command '/bin/sh -c exit 3' returned a non-zero code: 3
```

### `GET /v1/agents/{name}/image`

에이전트 현재 버전의 빌드 기록과, 빌드가 필요한지:

```json
{"agent": "docs-custom", "version": "0.1.2", "status": "failed",
 "image": "malkuth/agent-docs-custom:0.1.2", "needs_build": true,
 "error": "image build failed: malkuth/agent-docs-custom:0.1.2",
 "log": "Step 1/2 : FROM malkuth/agent-base:0.1.0\n ... returned a non-zero code: 3",
 "updated_at": "..."}
```

| `status` | 의미 |
|---|---|
| `null` | 구운 적이 없다 |
| `building` | 굽는 중 — 다시 조회한다 |
| `built` | 이미지가 있다 — 배포가 쓸 수 있다 |
| `failed` | 마지막 빌드가 실패했다 — `error` 와 `log` 를 읽는다 |

재료가 없는 에이전트는 `needs_build` 가 `false` 다. `log` 는 마지막 8000자를 보관한다. 기록은
control plane 재시작을 넘는다.

## 배포

배포는 그래프 하나를 실행 중인 컨테이너 집합으로 만든다. base 이미지는 선언을 갖고 있지
않으므로, runtime 이 매니페스트와 모듈 루트를 읽기 전용으로 마운트하고 그래프가 선언한
A2A 배선을 주입한다.

### `POST /v1/deployments`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graph": "research-pipeline"}' http://127.0.0.1:8700/v1/deployments
```

모든 에이전트가 healthy 를 보고하면 `201`:

```json
{
  "deployment_id": "dep-2fc94a0b5f24",
  "graph": "research-pipeline",
  "version": "1.0.0",
  "status": "ready",
  "error": null,
  "updated_at": "2026-09-13T11:29:06.391594+00:00",
  "agents": [
    {"name": "planner", "replica": 0, "container_id": "0f2edbe1aa5a",
     "image": "malkuth/agent-base:0.1.0", "control_port": 32852, "a2a_port": 9100}
  ]
}
```

이 호출은 에이전트가 healthy 가 되거나 기한이 지날 때까지 기다린다 — 레퍼런스 그래프에서는
수 초다. 에이전트별 토큰은 응답에 실리지 않는다.

`status` 는 다음 중 하나다:

| 상태 | 의미 |
|---|---|
| `starting` | 컨테이너가 올라오는 중 |
| `ready` | 모든 에이전트가 health 를 통과해 태스크를 받는다 |
| `failed` | 어느 단계가 실패했고, **이 배포가 띄운 것은 전부 다시 내려갔다** |
| `stopped` | `DELETE` 로 해체됨 |
| `lost` | 재시작 후 컨테이너 중 하나 이상이 사라져 있었다 |

실패해도 유령 컨테이너는 남지 않는다. 어떤 종류의 실패인지는 응답이 알려준다:

- `404` (`NF_001`) — 그런 그래프가 없다.
- `400` (`VAL_001`) — 그래프가 검증을 통과하지 못했다. 아무것도 기동하지 않았다.
- `400` (`VAL_002`) — 빌드 재료가 있는 에이전트가 빌드 태그(`malkuth/agent-<name>:<version>`)
  와 다른 `runtime.image` 를 선언했다. 두 곳이 다른 이미지를 가리키면 무엇이 도는지 알 수 없다.
- `409` (`RT_012`) — 빌드 재료가 있는 에이전트에 그 버전의 `built` 이미지가 없다: 굽힌 적이
  없거나, 마지막 빌드가 실패했거나, 아직 굽는 중이다. `details` 에 `image`, `build_status`,
  `build_error` 가 실린다. 구운 뒤 다시 배포한다 — 배포는 대신 굽지 않는다. 아무것도 기동하지
  않았고 배포 기록도 남지 않는다. 재료가 없는 에이전트는 base 이미지로 돌며 게이트를 거치지
  않는다.
- `409` (`RT_010`) — 이 그래프의 에이전트가 다른 배포로 이미 돌고 있다. 그쪽을 먼저
  해체한다 — 그래프와 그 에이전트는 한 단위로 배포되므로 두 배포가 에이전트를 공유할 수
  없다.
- `503` (`RT_002`) — 기한 안에 healthy 가 되지 못했다. retryable 로 표시된다: 컨테이너는
  되감겼으므로 다시 시도해도 안전하다.

### `GET /v1/deployments`, `GET /v1/deployments/{id}`

목록은 `{"items": [...]}`, 단건 조회는 기록 자체다. 둘 다 **지금 실제로 서 있는**
컨테이너를 반영한다 — 감시가 죽은 컨테이너를 다시 세웠다면 기록은 새 id 와 포트를 보여준다.

### `DELETE /v1/deployments/{id}`

에이전트마다 drain 을 청해 진행 중 태스크를 기다린 뒤 컨테이너를 정지·제거한다.
`status: "stopped"` 인 기록을 돌려주며, 이력을 읽을 수 있도록 기록 자체는 남긴다. 이미
`stopped` 나 `failed` 인 배포는 그대로 돌려준다.

## Run

Run 은 배포된 그래프를 구동한다. 배포가 에이전트의 위치를 알고 있으므로, run 제출에는
주소가 아니라 `deployment_id` 를 준다.

### `POST /v1/runs`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"deployment_id": "dep-2fc94a0b5f24", "input": {"query": "..."}}' \
  http://127.0.0.1:8700/v1/runs
```

즉시 `202` — run 은 수 분이 걸릴 수 있고, 그동안 HTTP 요청을 붙잡고 있어 봐야 누구에게도
도움이 되지 않는다:

```json
{"run_id": "run-a2bce8e90f40", "graph": "research-pipeline", "mode": "mission",
 "status": "running", "iteration": 0, "failure_streak": 0,
 "drain_requested": false, "updated_at": "..."}
```

`mode` 는 호출자가 아니라 그래프가 정한다. 선택 필드: 아이디를 지정하는 `run_id`, 그래프의
모드를 단언하는 `mode` (불일치는 `400`).

거절: 배포가 없거나 `ready` 가 아니면 `404` (`NF_001`). 디스크의 그래프가 배포된 것과 다른
버전으로 편집됐으면 `400` (`VAL_002`) — 먼저 재배포한다. 그러지 않으면 다른 선언으로 만든
컨테이너를 구동하게 된다.

### `GET /v1/runs`, `GET /v1/runs/{run_id}`

목록은 `?mode=mission|service` 를 받는다. 단건 조회는 **이 프로세스에서** 완주한 run 에
한해 두 필드를 더 싣는다 — `state` (최종 그래프 state) 와 `error`. Control plane 이
재시작하면 기록은 남지만 그 최종 state 는 남지 않는다. 지속되는 상태는 checkpointer 에 있다.

### `POST /v1/runs/{run_id}/drain`

현재 iteration 을 마친 뒤 정지하도록 요청하고 `drain_requested: true` 와 함께 즉시
돌아온다. run 은 스스로 `stopped` 에 도달하므로 `GET` 으로 확인한다. service run 을
멈추는 방법은 drain 이다 — 강제 종료는 없다.

### `POST /v1/runs/{run_id}/resume`

멈춘 자리에서 이어간다. 그 의미는 모드마다 다르다:

- **service run** 은 마지막 iteration 부터, 그리고 `halted` 에서만 이어간다 — service
  그래프가 연속 실패 임계를 넘어 도달하는 상태다 (`GRAPH_005`). 그 외의 상태는 `409`
  (`GRAPH_006`): 의도적으로 drain 한 run 은 재개가 아니라 새로 제출하는 것이 맞다.
- **mission run** 은 마지막 checkpoint 에서 이어가며 **상태 가드가 없다** — 무엇을 이어갈지는
  checkpointer 가 정한다. 이미 완주한 run 을 재개하면 그 checkpoint 부터 다시 구동된다.
  영속 checkpointer 가 없으면 이어갈 지점 자체가 없다 (`STOR_002`).

`501` 은 이 control plane 에 배포 표면이 없다는 뜻이다 — run 을 읽기만 하고 구동하지 않는데
`200` 을 주면 운영자가 재개됐다고 믿는다.

## 운영 시 알아 둘 것

- **에이전트 하나에 배포 하나.** 에이전트를 공유하는 두 그래프는 동시에 배포할 수 없다
  (`409`, `RT_010`).
- **배포 중에는 편집이 막힌다.** 의도된 동작이다 — 컨테이너는 특정 선언으로 만들어졌고,
  파일이 따로 흘러가면 그 배포를 재현할 수 없게 된다.
- **살아 있는 시스템을 수정한다**는 것은: 새 버전을 배포하고 옛것을 해체하는 것이다.
  실행 중 배포의 in-place 패치는 지원하지 않는다.
- **재시작하면 다시 붙는다.** 기동 시 기록과 Docker 를 대조해 컨테이너를 health 감시까지
  포함해 다시 잡는다. 컨테이너가 사라진 기록은 조용히 지우지 않고 `lost` 로 표시한다.

## CLI 대응

| API | CLI |
|---|---|
| `POST /v1/runs` | `malkuth run --deployment <id> --input '{...}'` |
| `GET /v1/runs` | `malkuth run-list [--mode service]` |
| `POST /v1/runs/{id}/drain` | `malkuth run-drain <id>` |
| `POST /v1/runs/{id}/resume` | `malkuth run-resume <id>` |
| `PUT /v1/agents/{name}/materials` | `malkuth agent-push <name> <directory>` |
| `POST` + `GET /v1/agents/{name}/image` | `malkuth agent-build <name> [--wait]` |

이 명령들은 `--control-url` 과 `--control-token` (또는 `MALKUTH_CONTROL_TOKEN`) 을 받는다.
`agent-push` 는 디렉토리를 읽되 `__pycache__` 같은 캐시 디렉토리는 건너뛰고 심볼릭 링크는
거절한다. `agent-build --wait` 은 빌드가 실패하면 로그 꼬리를 출력하고 0 이 아닌 코드로
끝난다.

`malkuth validate` 를 표에서 뺀 것은 의도적이다 — 저장소를 직접 읽는 **로컬** 명령이고
control plane 플래그를 받지 않는다. 저장하지 않은 초안을 검증하는 원격 대응이
`POST /v1/validate` 다. 전체 명령 레퍼런스는 [루트 README](../../README.md#commands) 참조.
