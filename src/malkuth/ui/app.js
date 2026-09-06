// 페이지 글루 — DOM 만 다룬다. REST 는 client.js 가, 선언 문서의 모양도 client.js 가 안다.
import { ApiError, bumpPatch, createClient, emptyAgent, emptyGraph, moduleRef, pruneEmpty } from "./client.js";

const $ = (selector) => document.querySelector(selector);
const el = (tag, props = {}, children = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) node.append(child);
  return node;
};

let api = createClient({ token: sessionStorage.getItem("malkuth.token") });
const status = (text, failed = false) => {
  const line = $("#status");
  line.textContent = text;
  line.className = failed ? "status-failed" : "muted";
};
const report = (err) => {
  status(err instanceof ApiError ? err.message : String(err), true);
  return null;
};

// --- 인증 / 탭 -------------------------------------------------------------------

$("#auth").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("#token").value.trim();
  sessionStorage.setItem("malkuth.token", token);
  api = createClient({ token: token || null });
  await connect();
});

async function connect() {
  try {
    await api.health();
    await api.graphs(); // 인증이 걸린 첫 호출 — 토큰이 틀리면 여기서 401
    $("#health").textContent = "연결됨";
    $("#health").className = "status-ready";
    await Promise.all([loadCatalog(), loadDeployments(), loadRuns()]);
  } catch (err) {
    $("#health").textContent = err instanceof ApiError && err.status === 401 ? "토큰 거부" : "연결 실패";
    $("#health").className = "status-failed";
    report(err);
  }
}

for (const button of document.querySelectorAll("#tabs button")) {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${button.dataset.tab}`));
  });
}

// --- 카탈로그 (#240) ------------------------------------------------------------

const catalog = { agents: [], graphs: [], groups: [], modules: {} };

async function loadCatalog() {
  const [agents, graphs, groups, skillsets, promptsets, memorysets] = await Promise.all([
    api.agents(), api.graphs(), api.groups(),
    api.modules("skillsets"), api.modules("promptsets"), api.modules("memorysets"),
  ]);
  catalog.agents = agents.items; catalog.graphs = graphs.items; catalog.groups = groups.items;
  catalog.modules = { skillsets: skillsets.items, promptsets: promptsets.items, memorysets: memorysets.items };

  fill("#catalog-agents", agents.items, (a) => `${a.name}@${a.version} — ${a.model.provider}/${a.model.name}`, (a) => api.agent(a.name));
  fill("#catalog-graphs", graphs.items, (g) => `${g.name}@${g.version} (${g.mode}, ${g.nodes} nodes)`, (g) => api.graph(g.name));
  fill("#catalog-groups", groups.items, (g) => g.name, (g) => Promise.resolve(g));
  const modules = Object.entries(catalog.modules).flatMap(([type, items]) => items.map((m) => ({ type, ...m })));
  fill("#catalog-modules", modules, (m) => `${m.type}/${m.name} @ ${m.versions.join(", ")}`, (m) => Promise.resolve(m));

  const problems = [...agents.problems, ...graphs.problems, ...groups.problems, ...skillsets.problems, ...promptsets.problems, ...memorysets.problems];
  $("#catalog-problems").replaceChildren(...problems.map((p) => el("li", { textContent: `${p.path}: ${p.code} ${p.message}` })));

  fillSelect("#deploy-graph", graphs.items.map((g) => [g.name, `${g.name}@${g.version}`]));
  fillSelect("select[name=group]", [["", "(없음)"], ...groups.items.map((g) => [g.name, g.name])]);
  fillSelect("select[name=promptset]", versionsOf("promptsets"));
  fillSelect("select[name=skillsets]", versionsOf("skillsets"));
  fillSelect("select[name=memorysets]", versionsOf("memorysets"));
}

function versionsOf(type) {
  return (catalog.modules[type] || []).flatMap((m) => m.versions.map((v) => [moduleRef(type, m.name, v), moduleRef(type, m.name, v)]));
}

function fill(selector, items, label, detail) {
  $(selector).replaceChildren(
    ...items.map((item) => el("li", { textContent: label(item), onclick: async () => {
      try { $("#catalog-detail").textContent = JSON.stringify(await detail(item), null, 2); $("#catalog-detail").className = "detail"; }
      catch (err) { report(err); }
    } })),
  );
}

function fillSelect(selector, pairs, keep = true) {
  const select = $(selector);
  const current = keep ? select.value : null;
  select.replaceChildren(...pairs.map(([value, text]) => el("option", { value, textContent: text })));
  if (current && [...select.options].some((o) => o.value === current)) select.value = current;
}

// --- 그래프 편집기 (#242) --------------------------------------------------------

const graphForm = $("#graph-form");
function syncServiceFields(isService) {
  // hidden 만으로는 부족하다 — required 필드가 mission 모드에서도 폼 제출을 막는다.
  // disabled 는 constraint validation 과 값 읽기 양쪽에서 그 필드를 뺀다
  const fieldset = $("#graph-service");
  fieldset.hidden = !isService;
  fieldset.disabled = !isService;
}
graphForm.mode.addEventListener("change", () => syncServiceFields(graphForm.mode.value === "service"));

const rowFactories = {
  node: () => [el("input", { name: "node_id", placeholder: "planner" }), el("select", { name: "node_agent" })],
  edge: () => [el("input", { name: "edge_from", placeholder: "START" }), el("input", { name: "edge_to", placeholder: "END" }),
    el("input", { name: "edge_condition", placeholder: "malkuth.graphs.conditions:needs_research" }), el("input", { name: "edge_max", type: "number", min: 1 })],
  connection: () => [el("input", { name: "conn_caller" }), el("input", { name: "conn_callee" })],
};
const tables = { node: "#graph-nodes", edge: "#graph-edges", connection: "#graph-connections" };

function addRow(kind, values = []) {
  const cells = rowFactories[kind]();
  cells.forEach((cell, i) => {
    if (cell.tagName === "SELECT") {
      cell.replaceChildren(...catalog.agents.map((a) => el("option", { value: `agents/${a.name}@${a.version}`, textContent: `${a.name}@${a.version}` })));
    }
    if (values[i] !== undefined && values[i] !== null) cell.value = values[i];
  });
  const remove = el("button", { type: "button", textContent: "✕", onclick: (e) => e.target.closest("tr").remove() });
  $(`${tables[kind]} tbody`).append(el("tr", {}, [...cells.map((c) => el("td", {}, [c])), el("td", {}, [remove])]));
}
for (const button of document.querySelectorAll("[data-add]")) button.addEventListener("click", () => addRow(button.dataset.add));

function rows(kind) {
  return [...document.querySelectorAll(`${tables[kind]} tbody tr`)].map((tr) => [...tr.querySelectorAll("input, select")].map((i) => i.value.trim()));
}

function graphDocument() {
  const f = graphForm;
  const doc = emptyGraph(f.name.value.trim());
  doc.metadata.version = f.version.value.trim();
  doc.metadata.description = f.description.value.trim();
  doc.spec.mode = f.mode.value;
  doc.spec.goal = f.goal.value.trim();
  doc.spec.state = { schema: f.state_schema.value.trim() };
  doc.spec.nodes = rows("node").map(([id, agent]) => ({ id, agent }));
  doc.spec.edges = rows("edge").map(([from, to, condition, max]) => pruneEmpty({ from, to, condition, max_iterations: max ? Number(max) : "" }));
  doc.spec.connections = rows("connection").map(([caller, callee]) => ({ caller, callee }));
  if (f.mode.value === "service") {
    doc.spec.service = { idle: { min_delay_s: Number(f.idle_min.value), max_delay_s: Number(f.idle_max.value) }, max_failure_streak: Number(f.failure_streak.value) };
  }
  return pruneEmpty(doc);
}

function showGraph(doc) {
  const f = graphForm;
  f.name.value = doc.metadata.name; f.version.value = doc.metadata.version; f.description.value = doc.metadata.description || "";
  f.mode.value = doc.spec.mode; f.goal.value = doc.spec.goal || ""; f.state_schema.value = doc.spec.state?.schema || "";
  syncServiceFields(doc.spec.mode === "service");
  if (doc.spec.service) { f.idle_min.value = doc.spec.service.idle.min_delay_s; f.idle_max.value = doc.spec.service.idle.max_delay_s; f.failure_streak.value = doc.spec.service.max_failure_streak ?? 5; }
  for (const kind of Object.keys(tables)) $(`${tables[kind]} tbody`).replaceChildren();
  (doc.spec.nodes || []).forEach((n) => addRow("node", [n.id, n.agent]));
  (doc.spec.edges || []).forEach((e) => addRow("edge", [e.from, e.to, e.condition || "", e.max_iterations || ""]));
  (doc.spec.connections || []).forEach((c) => addRow("connection", [c.caller, c.callee]));
}

function showFindings(selector, result) {
  const list = $(selector);
  if (result.ok) { list.replaceChildren(el("li", { className: "ok", textContent: "검증 통과" })); return true; }
  list.replaceChildren(...result.findings.map((f) => el("li", { textContent: `${f.check} [${f.code}] ${f.message}` })));
  return false;
}

async function validateGraph() {
  try { return showFindings("#graph-findings", await api.validate({ graphs: [graphDocument()] })); }
  catch (err) { showApiFindings("#graph-findings", err); return false; }
}
function showApiFindings(selector, err) {
  const errors = err instanceof ApiError && err.details.errors ? err.details.errors.map((e) => `${e.field}: ${e.problem}`) : [String(err.message || err)];
  $(selector).replaceChildren(...errors.map((text) => el("li", { textContent: text })));
}

$("#graph-validate").addEventListener("click", validateGraph);
$("#graph-load").addEventListener("click", async () => {
  const name = prompt("불러올 그래프 이름", catalog.graphs[0]?.name || "");
  if (!name) return;
  try { const doc = await api.graph(name); doc.metadata.version = bumpPatch(doc.metadata.version); showGraph(doc); status(`${name} 불러옴 — 저장하려면 버전이 올라가야 하므로 patch 를 올려 두었습니다`); }
  catch (err) { report(err); }
});
graphForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!(await validateGraph())) return; // 저장 전에 검증 실패가 화면에 보인다
  try {
    const doc = graphDocument();
    const saved = await api.saveGraph(doc.metadata.name, doc);
    status(`저장됨: ${saved.path}`);
    await loadCatalog();
  } catch (err) { showApiFindings("#graph-findings", err); report(err); }
});
$("#graph-delete").addEventListener("click", async () => {
  const name = graphForm.name.value.trim();
  if (!name || !confirm(`${name} 을 삭제할까요?`)) return;
  try { await api.deleteGraph(name); status(`삭제됨: ${name}`); await loadCatalog(); } catch (err) { report(err); }
});

// --- 에이전트 편집기 (#242) ------------------------------------------------------

const agentForm = $("#agent-form");
const selected = (select) => [...select.selectedOptions].map((o) => o.value);

function agentDocument() {
  const f = agentForm;
  const doc = emptyAgent(f.name.value.trim());
  doc.metadata.version = f.version.value.trim();
  doc.metadata.description = f.description.value.trim();
  doc.metadata.group = f.group.value;
  doc.spec.model = { provider: f.provider.value.trim(), name: f.model.value.trim() };
  doc.spec.promptset = { ref: f.promptset.value };
  doc.spec.skillsets = selected(f.skillsets).map((ref) => ({ ref }));
  const memory = selected(f.memorysets).map((ref) => ({ ref, as: ref.split("/")[1].split("@")[0] }));
  if (memory.length) doc.spec.memory = { spaces: memory };
  doc.spec.runtime = { image: f.image.value.trim(), env_allowlist: f.env_allowlist.value.split(",").map((s) => s.trim()).filter(Boolean) };
  if (f.a2a.checked) doc.spec.a2a = { enabled: true };
  return pruneEmpty(doc);
}

function showAgent(doc) {
  const f = agentForm;
  f.name.value = doc.metadata.name; f.version.value = doc.metadata.version; f.description.value = doc.metadata.description || "";
  f.group.value = doc.metadata.group || "";
  f.provider.value = doc.spec.model.provider; f.model.value = doc.spec.model.name;
  f.promptset.value = doc.spec.promptset?.ref || "";
  const skill = new Set((doc.spec.skillsets || []).map((s) => s.ref));
  [...f.skillsets.options].forEach((o) => { o.selected = skill.has(o.value); });
  const mem = new Set((doc.spec.memory?.spaces || []).map((s) => s.ref));
  [...f.memorysets.options].forEach((o) => { o.selected = mem.has(o.value); });
  f.image.value = doc.spec.runtime?.image || "";
  f.env_allowlist.value = (doc.spec.runtime?.env_allowlist || []).join(", ");
  f.a2a.checked = Boolean(doc.spec.a2a?.enabled);
}

async function validateAgent() {
  try { return showFindings("#agent-findings", await api.validate({ agents: [agentDocument()] })); }
  catch (err) { showApiFindings("#agent-findings", err); return false; }
}
$("#agent-validate").addEventListener("click", validateAgent);
$("#agent-load").addEventListener("click", async () => {
  const name = prompt("불러올 에이전트 이름", catalog.agents[0]?.name || "");
  if (!name) return;
  try { const doc = await api.agent(name); doc.metadata.version = bumpPatch(doc.metadata.version); showAgent(doc); status(`${name} 불러옴 (patch 버전 올림)`); }
  catch (err) { report(err); }
});
agentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!(await validateAgent())) return;
  try {
    const doc = agentDocument();
    const saved = await api.saveAgent(doc.metadata.name, doc);
    status(`저장됨: ${saved.path}`);
    await loadCatalog();
  } catch (err) { showApiFindings("#agent-findings", err); report(err); }
});
$("#agent-delete").addEventListener("click", async () => {
  const name = agentForm.name.value.trim();
  if (!name || !confirm(`${name} 을 삭제할까요?`)) return;
  try { await api.deleteAgent(name); status(`삭제됨: ${name}`); await loadCatalog(); } catch (err) { report(err); }
});

// --- 배포 (#243) ----------------------------------------------------------------

async function loadDeployments() {
  const { items } = await api.deployments();
  $("#deployments tbody").replaceChildren(...items.map((d) => el("tr", {}, [
    el("td", { textContent: d.deployment_id }),
    el("td", { textContent: `${d.graph}@${d.version}` }),
    el("td", { textContent: d.status, className: `status-${d.status}` }),
    el("td", { textContent: d.agents.map((a) => `${a.name}#${a.replica} (${a.container_id})`).join(", ") || (d.error || "") }),
    el("td", {}, d.status === "ready" || d.status === "lost" ? [el("button", { textContent: "해체", className: "danger", onclick: async () => {
      try { await api.teardown(d.deployment_id); status(`해체됨: ${d.deployment_id}`); await Promise.all([loadDeployments(), loadRunTargets()]); } catch (err) { report(err); }
    } })] : []),
  ])));
  await loadRunTargets(items);
}
$("#deploy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const graph = $("#deploy-graph").value;
  status(`배포 중: ${graph} …`);
  try { const d = await api.deploy(graph); status(`배포됨: ${d.deployment_id} (${d.status})`); await loadDeployments(); }
  catch (err) { report(err); await loadDeployments(); }
});

async function loadRunTargets(items) {
  const ready = (items || (await api.deployments()).items).filter((d) => d.status === "ready");
  fillSelect("#run-deployment", ready.map((d) => [d.deployment_id, `${d.deployment_id} — ${d.graph}@${d.version}`]));
}

// --- run (#244) -----------------------------------------------------------------

async function loadRuns() {
  const items = await api.runs();
  $("#runs tbody").replaceChildren(...items.map((r) => el("tr", {}, [
    el("td", { textContent: r.run_id }),
    el("td", { textContent: r.graph }),
    el("td", { textContent: r.mode }),
    el("td", { textContent: r.status, className: `status-${r.status}` }),
    el("td", { textContent: String(r.iteration) }),
    el("td", {}, [
      el("button", { textContent: "보기", onclick: async () => { try { const full = await api.run(r.run_id); $("#run-detail").textContent = JSON.stringify(full, null, 2); $("#run-detail").className = "detail"; } catch (err) { report(err); } } }),
      ...(r.mode === "service" && r.status === "running" ? [el("button", { textContent: "drain", onclick: async () => { try { await api.drain(r.run_id); status(`drain 요청: ${r.run_id}`); await loadRuns(); } catch (err) { report(err); } } })] : []),
      ...(r.status === "halted" || r.status === "failed" ? [el("button", { textContent: "재개", onclick: async () => { try { await api.resume(r.run_id); status(`재개: ${r.run_id}`); await loadRuns(); } catch (err) { report(err); } } })] : []),
    ]),
  ])));
}
$("#run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  let input;
  try { input = JSON.parse($("#run-input").value || "{}"); } catch (err) { return report(new Error(`input 이 JSON 이 아닙니다: ${err.message}`)); }
  try {
    const submitted = await api.submit($("#run-deployment").value, input);
    status(`제출됨: ${submitted.run_id}`);
    await loadRuns();
    watch(submitted.run_id);
  } catch (err) { report(err); }
});
function watch(runId) {
  const tick = async () => {
    try {
      const current = await api.run(runId);
      await loadRuns();
      if (current.status === "running") setTimeout(tick, 2000);
      else { $("#run-detail").textContent = JSON.stringify(current, null, 2); $("#run-detail").className = "detail"; status(`${runId}: ${current.status}`); }
    } catch (err) { report(err); }
  };
  setTimeout(tick, 1000);
}

// --- 시작 ---------------------------------------------------------------------

$("#token").value = sessionStorage.getItem("malkuth.token") || "";
addRow("edge", ["START", ""]);
connect();
