# Testing and Quality Assurance Rules

## Testing Philosophy

### Core Principles

1. **Test Coverage**
   - Minimum 70% code coverage for core packages
   - 90%+ coverage for critical paths (`core/`, `orchestrator/`, protocol boundaries)
   - 100% coverage for error handling paths (boundary 변환 로직)

2. **Test Pyramid**
   ```
        E2E Tests (5%)
       ┌─────────────┐
       │Integration  │ (25%)
       ├─────────────┤
       │   Unit      │ (70%)
       └─────────────┘
   ```

3. **Determinism Around Non-Determinism**
   - 테스트는 실제 LLM 을 호출하지 않는다 — fake model / 기록된 응답 사용
   - 테스트는 외부 서비스에 의존하지 않는다 — 컨테이너 fixture 또는 mock
   - 테스트는 결정적이고 병렬 실행 가능해야 한다

4. **Test Naming**
   ```python
   # Pattern: test_<function>_<scenario>_<expected>
   def test_invoke_valid_task_returns_completed(): ...
   def test_invoke_timeout_returns_to_001(): ...
   def test_call_tool_transport_lost_raises_retryable_mcp_004(): ...
   ```

## Test Organization

### Directory Structure

모든 테스트는 `tests/` 아래에 위치하며 **소스 구조를 미러링**한다. `src/` 안에 테스트 파일을
두지 않는다.

```
tests/
├── unit/                          # 외부 의존 없음, 빠름
│   ├── core/
│   │   ├── test_manifest.py       # ← src/malkuth/core/manifest.py
│   │   └── test_errors.py
│   ├── orchestrator/
│   │   ├── test_builder.py
│   │   └── test_topology.py
│   ├── protocols/
│   │   ├── a2a/test_client.py
│   │   └── mcp/test_client.py
│   ├── modules/
│   │   ├── test_skillset.py
│   │   └── test_promptset.py
│   └── agentd/test_executor.py
│
├── integration/                   # Docker/실서버 fixture 사용, 느림
│   ├── runtime/test_docker_lifecycle.py
│   ├── protocols/test_mcp_stdio_session.py
│   └── orchestrator/test_graph_run.py
│
├── e2e/                           # 전체 스택 (compose) — CI nightly
│   └── test_research_pipeline.py
│
├── fixtures/                      # 공유 fixture / builder / fake
│   ├── fake_model.py
│   ├── fake_mcp_server.py
│   ├── builders.py
│   └── manifests/                 # 테스트용 manifest/graph yaml
└── conftest.py
```

- 마커: `@pytest.mark.integration`, `@pytest.mark.e2e` — 기본 실행(`make test`)은 unit 만,
  `make test-integration` / `make test-e2e` 로 분리 실행

### Frameworks

1. **Runner**: pytest + pytest-asyncio (`asyncio_mode = "auto"`)
2. **Assertions**: 표준 assert (pytest rewriting)
3. **Mocking**: `unittest.mock` / pytest fixture — 남용 금지, 계약 경계에서만
4. **Containers**: testcontainers-python
5. **Coverage**: pytest-cov

## Unit Testing

### What to Test

1. **core/**: manifest/topology 스키마 검증 (유효/무효 케이스 전수), 에러 타입 동작
2. **orchestrator/**: config → StateGraph 빌드 결과, edge 조건 라우팅, state 병합 규칙,
   토폴로지 검증기의 각 실패 케이스
3. **protocols/**: 에러 변환 (원 예외 → MalkuthError 카테고리/코드/retryable),
   allowlist 검사, tool 네임스페이싱
4. **modules/**: ref 파싱/해석, skillset schema 자동 생성, promptset 변수 검증·렌더링
5. **agentd/**: tool loop (max turns, 병렬 tool, usage 집계), cancellation 처리

### Fake Model — LLM 호출 테스트의 표준

```python
class FakeModel:
    """스크립트된 응답을 순서대로 반환하는 모델 대역."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[ModelCall] = []       # 검증용 호출 기록

    async def run(self, prompt: Prompt, tools: list[Tool]) -> ModelResponse:
        self.calls.append(ModelCall(prompt=prompt, tools=tools))
        return next(self._responses)


async def test_executor_runs_tool_then_completes():
    model = FakeModel([
        ModelResponse.tool_call("mcp__fs__read_file", {"path": "a.txt"}),
        ModelResponse.text("done"),
    ])
    executor = Executor(model=model, tools=[fake_read_file])

    result = await executor.execute(make_task(input={"query": "read a"}))

    assert result.status == TaskStatus.COMPLETED
    assert len(model.calls) == 2
```

- 실제 provider SDK 호출 금지 — CI 에서 API key 부재로 실패해야 정상
- 실 응답 기반 회귀가 필요하면 기록/재생(cassette) fixture 를 `tests/fixtures/` 에 저장

### Test Data — Builders and Fixtures

```python
# tests/fixtures/builders.py
def make_manifest(**overrides: Any) -> AgentManifest:
    base = AgentManifest(
        metadata=Metadata(name="test-agent", version="0.1.0"),
        spec=Spec(
            model=ModelConfig(provider="anthropic", name="claude-sonnet-5"),
            promptset=Ref("promptsets/test@0.1.0"),
        ),
    )
    return base.model_copy(update=overrides)


def make_task(**overrides: Any) -> TaskRequest: ...
def make_graph(nodes: list[str], edges: list[tuple[str, str]]) -> GraphTopology: ...
```

- 테스트용 yaml manifest/graph 는 `tests/fixtures/manifests/` 에 — 실제 스키마로 로드해
  사용 (스키마와 fixture 의 드리프트 방지)

### Table-Driven Tests

다중 시나리오는 `pytest.mark.parametrize` 사용:

```python
@pytest.mark.parametrize(
    ("ref", "expected_type", "expected_name", "expected_version"),
    [
        ("skillsets/web-search@0.2.0", "skillsets", "web-search", "0.2.0"),
        ("agents/planner@1.0.0", "agents", "planner", "1.0.0"),
    ],
)
def test_parse_ref_valid(ref, expected_type, expected_name, expected_version):
    parsed = ModuleRef.parse(ref)
    assert (parsed.type, parsed.name, parsed.version) == (
        expected_type, expected_name, expected_version)


@pytest.mark.parametrize("ref", ["web-search", "skillsets/x@latest", "skillsets/@1.0.0"])
def test_parse_ref_invalid_raises(ref):
    with pytest.raises(MalkuthError) as exc_info:
        ModuleRef.parse(ref)
    assert exc_info.value.code == "MOD_001"
```

## Integration Testing

### Docker Runtime Tests

```python
@pytest.mark.integration
async def test_agent_container_lifecycle(docker_runtime):
    manifest = load_fixture_manifest("echo-agent")

    handle = await docker_runtime.start(manifest)
    try:
        health = await wait_for_ready(docker_runtime, handle, timeout=30)
        assert health.status == "healthy"

        result = await docker_runtime.invoke(handle, make_task(input={"msg": "hi"}))
        assert result.status == TaskStatus.COMPLETED
    finally:
        await docker_runtime.stop(handle)
```

- 테스트 전용 경량 에이전트 이미지 (`malkuth/agent-echo`) 를 fixture 로 유지 —
  모델 호출 없이 입력을 echo (프로토콜/lifecycle 검증용)
- 테스트 후 컨테이너/네트워크 정리 보장 (fixture finalizer)

### MCP Session Tests

```python
@pytest.mark.integration
async def test_mcp_stdio_session_lists_tools(tmp_path):
    config = McpServerConfig(
        name="fs", transport="stdio",
        command=["mcp-server-filesystem", str(tmp_path)],
    )

    async with McpSession.launch(config, agent_name="test") as session:
        tools = await session.list_tools()
        assert any(t.name == "mcp__fs__read_file" for t in tools)
```

- 실제 참조 MCP 서버(filesystem 등)로 세션 수립/단절/재연결 시나리오 검증
- Fake MCP 서버 (`tests/fixtures/fake_mcp_server.py`) 로 오류 응답/지연 시나리오 검증

### Graph-Level Tests

그래프 라우팅은 **컨테이너 없이** 검증한다 — runtime 을 fake 로 치환:

```python
@pytest.mark.integration
async def test_graph_conditional_routing(fake_runtime):
    fake_runtime.script("planner", output={"plan": "...", "needs_research": False})
    graph = build_graph(load_fixture_graph("research-pipeline"), runtime=fake_runtime)

    final_state = await graph.ainvoke({"query": "q"})

    assert fake_runtime.invoked == ["planner"]     # researcher 스킵 확인
    assert "plan" in final_state
```

- Checkpointer 는 in-memory 사용 (`MemorySaver`)
- 재개 시나리오: node 실패 → 동일 checkpoint 에서 resume → 성공 경로 검증 필수

## End-to-End Testing

```python
@pytest.mark.e2e
async def test_research_pipeline_full_stack(compose_stack):
    """compose 로 전체 스택 기동 후 실제 그래프 run 검증. 모델은 fake provider 컨테이너."""
    client = MalkuthClient(compose_stack.control_plane_url)

    run = await client.submit("research-pipeline", {"query": "test"})
    result = await client.wait(run.run_id, timeout=120)

    assert result.status == "completed"
    assert result.state["report"]
```

- E2E 에서도 실제 LLM 금지 — OpenAI/Anthropic 호환 fake provider 컨테이너 사용
- CI 에서는 nightly 로만 실행 (PR gate 는 unit + integration)

## Testing Async / Concurrent Code

1. `pytest-asyncio` auto mode — `async def test_*` 그대로 작성
2. 시간 의존 로직(백오프, health 주기)은 sleep 하지 않는다 — clock 주입 또는
   `delay_fn` 파라미터화로 즉시 진행
3. Cancellation 테스트 필수 경로: 태스크 취소 시 tool 정리, drain 중 신규 태스크 거부
4. 동시성 검증: `asyncio.gather` 병렬 tool 실행, 세마포어 상한 동작

```python
async def test_cancel_inflight_task_cleans_up_tools():
    started, cleaned = asyncio.Event(), asyncio.Event()
    executor = Executor(model=slow_model(started), on_cleanup=cleaned.set)

    task = asyncio.create_task(executor.execute(make_task()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
```

## Module Tests

### Skillset

- skill 함수 단위 테스트 (SkillContext 는 fake)
- **Schema snapshot**: 시그니처 → tool schema 자동 생성 결과를 스냅샷으로 고정 —
  의도치 않은 스키마 변경(=모델이 보는 계약 변경) 감지

### Promptset

- 변수 검증: 필수 변수 누락 시 `MOD_004`
- **Golden test**: 대표 입력에 대한 렌더 결과를 golden 파일로 고정 —
  프롬프트 변경이 diff 로 보이게 (버전 bump 강제와 짝을 이룸)

### Graph

- 토폴로지 검증기: dangling edge / 미도달 노드 / cycle without max_iterations 등
  각 실패 규칙별 케이스
- fake runtime 라우팅 시나리오 (위 Graph-Level Tests)

## Benchmarking

- 프레임워크 오버헤드 벤치마크: Control API 왕복, state 병합, 템플릿 렌더
- `pytest-benchmark` 사용, 회귀 임계 초과 시 CI 경고

```python
def test_bench_topology_validation(benchmark):
    topology = load_fixture_graph("large-50-nodes")
    benchmark(validate_topology, topology)
```

## Quality Gates

### Linting & Type Check

```toml
# pyproject.toml (발췌)
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "ASYNC", "S", "C4", "RET", "SIM", "PTH"]

[tool.mypy]
python_version = "3.12"
strict = true                      # src/malkuth/core 는 strict 필수
```

### Pre-Merge Checklist (CI 강제)

```bash
make lint        # ruff check + ruff format --check
make typecheck   # mypy
make test        # pytest tests/unit (커버리지 포함)
make test-integration   # PR gate (Docker 가용 환경)
```

- Coverage < 70% 시 CI 실패
- 신규 boundary 코드에 에러 경로 테스트 없으면 리뷰에서 반려

### CI Workflow

```yaml
# .github/workflows/test.yml (요지)
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --frozen
      - run: make lint typecheck
      - run: make test              # unit + coverage gate
      - run: make test-integration  # Docker 기반
  e2e-nightly:
    if: github.event_name == 'schedule'
    steps:
      - run: make test-e2e
```

## Smoke Testing

배포 직후 실행하는 최소 검증:

1. `malkuth status` — 모든 에이전트 healthy
2. 각 그래프의 canary run 1회 (fake 입력) — END 도달 확인
3. 메트릭 endpoint 응답 확인

## Test Documentation

- 테스트 전략/시나리오 문서: `docs/en/testing.md` (+ `docs/ko/`)
- Known limitations 추적: 재현 불가 케이스, 실 LLM 에서만 발생하는 이슈 등을
  문서화하고 관련 테스트 이름을 연결
