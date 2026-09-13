// Malkuth control plane 클라이언트 — DOM 을 모른다. 페이지(app.js)와 테스트가 같이 쓴다.
// 규칙: 파일 경로도 컨테이너 런타임 호출도 없다. /v1/* REST 만 부른다 (#245).

export class ApiError extends Error {
  constructor(status, payload) {
    const error = payload && payload.error ? payload.error : null;
    super(error ? `${error.code}: ${error.message}` : `HTTP ${status}`);
    this.status = status;
    this.code = error ? error.code : null;
    this.details = error ? error.details || {} : {};
  }
}

export function createClient({ baseUrl = "", token = null, fetchImpl = globalThis.fetch } = {}) {
  const headers = () => {
    const h = { "content-type": "application/json" };
    if (token) h.authorization = `Bearer ${token}`;
    return h;
  };

  async function call(method, path, body) {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      method,
      headers: headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
  }

  return {
    health: () => call("GET", "/v1/health"),
    // 카탈로그 (#240)
    agents: () => call("GET", "/v1/agents"),
    agent: (name) => call("GET", `/v1/agents/${encodeURIComponent(name)}`),
    graphs: () => call("GET", "/v1/graphs"),
    graph: (name) => call("GET", `/v1/graphs/${encodeURIComponent(name)}`),
    groups: () => call("GET", "/v1/groups"),
    modules: (type) => call("GET", `/v1/modules/${encodeURIComponent(type)}`),
    module: (type, name, version) =>
      call("GET", `/v1/modules/${encodeURIComponent(type)}/${encodeURIComponent(name)}/${encodeURIComponent(version)}`),
    // 저작 (#242)
    validate: ({ graphs = [], agents = [] }) => call("POST", "/v1/validate", { graphs, agents }),
    saveGraph: (name, document) => call("PUT", `/v1/graphs/${encodeURIComponent(name)}`, document),
    deleteGraph: (name) => call("DELETE", `/v1/graphs/${encodeURIComponent(name)}`),
    saveAgent: (name, document) => call("PUT", `/v1/agents/${encodeURIComponent(name)}`, document),
    deleteAgent: (name) => call("DELETE", `/v1/agents/${encodeURIComponent(name)}`),
    // 배포 (#243)
    deployments: () => call("GET", "/v1/deployments"),
    deploy: (graph) => call("POST", "/v1/deployments", { graph }),
    teardown: (id) => call("DELETE", `/v1/deployments/${encodeURIComponent(id)}`),
    // run (#244)
    runs: () => call("GET", "/v1/runs"),
    run: (id) => call("GET", `/v1/runs/${encodeURIComponent(id)}`),
    submit: (deploymentId, input, runId) =>
      call("POST", "/v1/runs", { deployment_id: deploymentId, input, run_id: runId || null }),
    drain: (id) => call("POST", `/v1/runs/${encodeURIComponent(id)}/drain`),
    resume: (id) => call("POST", `/v1/runs/${encodeURIComponent(id)}/resume`),
  };
}

// --- 편집기의 순수 함수: 폼 상태 ↔ 선언 문서 --------------------------------------

export function emptyGraph(name = "") {
  return {
    apiVersion: "malkuth/v1",
    kind: "Graph",
    metadata: { name, version: "0.1.0", description: "" },
    spec: {
      mode: "mission",
      goal: "",
      state: { schema: "malkuth.graphs.schemas:ResearchState" },
      nodes: [],
      edges: [{ from: "START", to: "" }],
      connections: [],
    },
  };
}

export function emptyAgent(name = "") {
  return {
    apiVersion: "malkuth/v1",
    kind: "Agent",
    metadata: { name, version: "0.1.0", description: "" },
    spec: {
      model: { provider: "anthropic", name: "claude-sonnet-5" },
      promptset: { ref: "" },
      skillsets: [],
      runtime: { env_allowlist: ["ANTHROPIC_API_KEY"] },
    },
  };
}

// 편집기 폼의 텍스트 필드는 빈 문자열을 남긴다 — 선언에서는 없는 값이어야 한다
export function pruneEmpty(value) {
  if (Array.isArray(value)) return value.map(pruneEmpty);
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (v === "" || v === null || v === undefined) continue;
      out[k] = pruneEmpty(v);
    }
    return out;
  }
  return value;
}

// 노드 표의 한 칸(`key=source, key2=source2`)과 선언의 매핑 사이를 옮긴다.
// 편집기가 이 값을 만들지 못하면 노드는 빈 입력을 받고, 템플릿의 필수 변수가
// 채워지지 않아 run 이 시작된 뒤에야 MOD_004 로 죽는다 (#260).
export function parsePairs(text) {
  const pairs = {};
  for (const chunk of String(text || "").split(",")) {
    const item = chunk.trim();
    if (!item) continue;
    const at = item.indexOf("=");
    if (at < 0) continue;
    const key = item.slice(0, at).trim();
    const value = item.slice(at + 1).trim();
    if (key && value) pairs[key] = value;
  }
  return pairs;
}

export function formatPairs(pairs) {
  return Object.entries(pairs || {})
    .map(([key, value]) => `${key}=${value}`)
    .join(", ");
}

// 노드 하나의 기본 input_map — 템플릿의 필수 변수를 같은 이름의 state 필드에서 끌어온다.
// 대부분의 배선이 이 모양이므로 편집기가 미리 채워 준다.
export function suggestInputMap(required) {
  return Object.fromEntries((required || []).map((name) => [name, `state.${name}`]));
}

export function moduleRef(type, name, version) {
  return `${type}/${name}@${version}`;
}

export function bumpPatch(version) {
  const parts = String(version).split(".").map((p) => parseInt(p, 10));
  if (parts.length !== 3 || parts.some(Number.isNaN)) return version;
  parts[2] += 1;
  return parts.join(".");
}
