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

export function moduleRef(type, name, version) {
  return `${type}/${name}@${version}`;
}

export function bumpPatch(version) {
  const parts = String(version).split(".").map((p) => parseInt(p, 10));
  if (parts.length !== 3 || parts.some(Number.isNaN)) return version;
  parts[2] += 1;
  return parts.join(".");
}
