/* REAPER Run Cockpit — client. Pure vanilla, no build step, WS + REST. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nows = () => new Date().toLocaleTimeString();

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
const state = {
  run: null, functions: new Map(), sessions: new Map(),
  claims: [], tasks: [], loops: null, files: "",
  totals: {}, feed: [], feedPaused: false, ddPaused: false,
  seq: 0, updated: 0,
  health: { calls: 0, errors: 0, durSum: 0, maxDur: 0, slowest: [], errList: [] },
};

async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

// ---------------------------------------------------------------------------
// web socket
// ---------------------------------------------------------------------------
let ws = null, wsReconnect = 0;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { setWS("connected", "ok"); wsReconnect = 0; };
  ws.onmessage = (ev) => { try { handle(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => {
    setWS("reconnecting…", "dead");
    setTimeout(connectWS, Math.min(500 * Math.pow(2, wsReconnect++), 10000));
  };
  ws.onerror = () => ws.close();
}
function setWS(txt, cls) { const el = $("ws-state"); el.textContent = txt;
  el.className = "ws " + (cls || "conn"); }

// ---------------------------------------------------------------------------
// event routing
// ---------------------------------------------------------------------------
function handle(ev) {
  state.updated++;
  $("ev-count").textContent = `events: ${state.updated.toLocaleString()}`;
  if (!ev || !ev.event) return;
  recordFeed(ev);
  state.totals[ev.event] = (state.totals[ev.event] || 0) + 1;
  const d = ev.data || {};
  switch (ev.event) {
    case "run.started": {
      state.run = { run_id: d.run_id, binary: d.binary, phases: d.phases || [],
                    status: "running", error: null };
      setStatus("running");
      renderPhases();
      break;
    }
    case "run.phase":
      if (state.run) { state.run.phases_status[d.phase] = d.status; renderPhases(); }
      break;
    case "run.complete":
      if (state.run) { state.run.status = "complete";
        state.run.coverage = d.coverage || {}; }
      setStatus("complete"); renderCoverage();
      break;
    case "run.error":
      if (state.run) state.run.status = "error";
      setStatus("error");
      break;
    case "completion_coverage":
      state.run = state.run || {}; state.run.coverage = d;
      renderCoverage();
      break;
    case "llm.request":
      upsertSession(ev.session, { status: "in-flight", level: d.thinking_level,
                                  ts: ev.ts, max: d.max_completion_tokens });
      break;
    case "llm.response":
      addTurn(ev.session, { status: "ok", level: d.thinking_level,
        prompt: lastUser(d.messages), content: d.content, reasoning: d.reasoning,
        duration: d.duration_s, retries: d.n_retries, usage: d.usage, ts: ev.ts });
      tickHealth(d);
      break;
    case "llm.error":
      addTurn(ev.session, { status: "error", level: d.thinking_level,
        error: d.error, ts: ev.ts });
      state.health.errors++; state.health.errList.unshift(
        { session: ev.session, error: d.error, ts: nows() });
      if (state.health.errList.length > 50) state.health.errList.pop();
      break;
    case "claim_accepted": case "claim_rejected": case "claim_force_accepted":
      if ($("drawer").getAttribute("data-open") === "claims") refreshClaims();
      break;
    case "graph.query":
      // used by feed/health; ignore otherwise
      break;
    default:
      if (["resynthesis_iteration","investigation_stuck","investigation_agent",
           "scheduler_reviewed","pass2_review_retry"].includes(ev.event)) {
        if ($("drawer").getAttribute("data-open") === "loops") refreshLoops();
      }
  }
}
function lastUser(messages) {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i--)
    if (messages[i] && messages[i].role === "user")
      return String(messages[i].content);
  return "";
}
function tickHealth(d) {
  const h = state.health;
  h.calls++; h.durSum += (d.duration_s || 0);
  if ((d.duration_s || 0) > h.maxDur) h.maxDur = d.duration_s;
  if (d.duration_s > 120) {
    h.slowest.unshift({ session: "", dur: d.duration_s, level: d.thinking_level,
                        ts: nows() });
    if (h.slowest.length > 30) h.slowest.pop();
  }
}

// ---- sessions / dialogues --------------------------------------------------
function upsertSession(id, patch) {
  let s = state.sessions.get(id);
  if (!s) { s = { id, turns: [], status: "idle" }; state.sessions.set(id, s); }
  Object.assign(s, patch);
  s.last = Date.now();
  renderDialogues();
}
function addTurn(id, turn) {
  let s = state.sessions.get(id);
  if (!s) { s = { id, turns: [], status: "idle" }; state.sessions.set(id, s); }
  s.turns.push(turn); s.status = turn.status; s.last = Date.now();
  if (s.turns.length > 50) s.turns.shift();
  hintHot(id);
  renderDialogues();
}
function hintHot(sessionId) {
  // session ids often embed the function address; flash it on the board
  const m = /0x[0-9a-f]{4,}/i.exec(sessionId || "");
  if (!m) return;
  const el = document.querySelector(`.trow[data-addr="${m[0]}"]`);
  if (el) { el.classList.remove("hot"); void el.offsetWidth; el.classList.add("hot"); }
}

// ---- phases / coverage -----------------------------------------------------
const PHASES = [2, 3, 4, 5, 6, 7];
function renderPhases() {
  $("phases").innerHTML = "";
  const base = (state.run && state.run.phases) || PHASES;
  for (const p of base) {
    const st = (state.run && state.run.phases_status && state.run.phases_status[p])
      || "pending";
    const el = document.createElement("span");
    el.className = `phase ${st}`; el.textContent = `phase ${p} · ${st}`;
    $("phases").appendChild(el);
  }
}
function renderCoverage() {
  const c = (state.run && state.run.coverage) || {};
  const cov = c;
  if (!cov || !cov.renamable_functions) {
    $("cov-text").textContent = state.run && state.run.status === "complete"
      ? "complete (no coverage data)" : "no run yet";
    return;
  }
  $("cov-text").textContent =
    `${cov.with_claims||0}/${cov.renamable_functions||0} fns with claims · ` +
    `${cov.with_mid_confidence_or_above||0} mid-conf+ · ` +
    `${cov.speculation_only||0} speculation · ${cov.no_claims||0} no claims`;
}

// ---- rename board ----------------------------------------------------------
async function refreshFunctions() {
  try {
    const data = await getJSON("/api/functions");
    if (data.functions) {
      for (const f of data.functions) state.functions.set(f.address, f);
      renderBoard();
    }
  } catch (e) {}
}
function renderBoard() {
  const q = ($("fn-filter").value || "").toLowerCase();
  const list = [...state.functions.values()].sort(
    (a, b) => ((a.traversal_order ?? 1e9) - (b.traversal_order ?? 1e9)));
  const shown = q ? list.filter(f =>
    (f.address||"").toLowerCase().includes(q) ||
    (f.name||"").toLowerCase().includes(q) ||
    (f.canon_name||"").toLowerCase().includes(q) ||
    (f.llm_name||"").toLowerCase().includes(q)) : list;
  $("fn-count").textContent = `${shown.length}/${list.length}`;
  const head = `<div class="trow head">
    <span>address</span><span>original</span><span>recovered name</span>
    <span>level</span><span>status</span></div>`;
  const rows = shown.map(f => {
    const renamed = !!(f.canon_name || f.llm_name);
    const chip = f.pinned ? `<span class="chip pinned">pinned</span>`
      : renamed ? `<span class="chip renamed">renamed</span>`
      : `<span class="chip">pending</span>`;
    const name = (f.canon_name || f.llm_name || f.name || "").slice(0, 64);
    const orig = (f.name || "").slice(0, 40);
    return `<div class="trow" data-addr="${esc(f.address)}">
      <span class="addr">${esc(f.address)}</span>
      <span>${esc(orig)}</span>
      <span title="${esc(name)}">${esc(name) || "—"}</span>
      <span class="mono-pin">${f.traversal_order ?? "—"}</span>
      <span>${chip}</span></div>`;
  }).join("");
  $("rename-list").innerHTML = head + rows;
}

// ---- dialogues -------------------------------------------------------------
function renderDialogues() {
  $("dd-count").textContent = `${state.sessions.size} sessions`;
  const el = $("dialogue-list");
  if (state.sessions.size === 0) {
    el.innerHTML = `<div class="muted pad">waiting for LLM turns…</div>`;
    return;
  }
  const sessions = [...state.sessions.values()]
    .sort((a, b) => (b.last || 0) - (a.last || 0)).slice(0, 40);
  const pause = state.ddPaused;
  const html = sessions.map(s => {
    const last = s.turns[s.turns.length - 1];
    const st = last ? (last.status === "ok" ? "ok" :
      last.status === "error" ? "err" : "inf") : "inf";
    const dur = last && last.duration != null ? `${last.duration}s` : "";
    const level = last && last.level ? `<span class="tlevel">${esc(last.level)}</span>` : "";
    const bodies = s.turns.map(t => turnBody(t)).join("");
    return `<div class="turn" data-session="${esc(s.id)}">
      <div class="turn-head">
        <span class="ttag">${esc(s.id)}</span>${level}
        <span class="tlevel">${s.turns.length} turn(s)</span>
        <span class="dur">${dur}</span></div>
      <div class="turn-body st-${st}">${bodies}</div></div>`;
  }).join("");
  el.innerHTML = html;
  if (!pause) el.scrollTop = el.scrollHeight;
}
function turnBody(t) {
  let out = "";
  if (t.status === "error")
    out += `<div class="st-err">✕ ${esc(t.error || "error")} @ ${esc(t.ts||"")}</div>`;
  if (t.prompt)
    out += `<details><summary>prompt (${(t.prompt.length)} chars)</summary>
      <div class="qtext">${esc(t.prompt.slice(0, 4000))}</div></details>`;
  if (t.reasoning)
    out += `<details open><summary>thinking (${(t.reasoning.length)} chars)</summary>
      <div class="reasoning">${esc(t.reasoning.slice(0, 8000))}</div></details>`;
  if (t.content)
    out += `<details ${t.status === "ok" ? "" : "open"}>
      <summary>response (${(t.content.length)} chars)</summary>
      <div class="atext">${esc(t.content.slice(0, 6000))}</div></details>`;
  return out;
}

// ---- trace feed ------------------------------------------------------------
function recordFeed(ev) {
  state.feed.push(ev);
  if (state.feed.length > 1500) state.feed.splice(0, state.feed.length - 1500);
  if ($("drawer").getAttribute("data-open") === "feed") renderFeed();
}
function renderFeed() {
  const fq = ($("feed-query") && $("feed-query").value || "").toLowerCase();
  const ft = ($("feed-type") && $("feed-type").value || "").toLowerCase();
  const rows = state.feed.filter(e =>
    (!ft || e.event.toLowerCase().includes(ft)) &&
    (!fq || JSON.stringify(e.data || "").toLowerCase().includes(fq)));
  const html = rows.slice(-800).map(e => {
    const d = e.data || {};
    let meta = "";
    if (e.event === "graph.query") meta = esc((d.query||"").slice(0,140)) +
      ` <span class="muted">${d.duration_s !== undefined ? d.duration_s+"s" : ""}</span>`;
    else meta = esc(JSON.stringify(d).slice(0, 200));
    return `<div class="feedline"><span class="fseq">${e.seq}</span>
      <span class="fev">${esc(e.event)}</span>
      <span class="muted">${esc(e.session||"")}</span>
      <span>${meta}</span></div>`;
  }).join("");
  const box = $("feed-list");
  if (box) { box.innerHTML = html;
    if (!$("feed-follow") || $("feed-follow").checked) box.scrollTop = box.scrollHeight; }
}

// ---- drawers ---------------------------------------------------------------
function openDrawer(kind) {
  $("drawer").classList.remove("hidden");
  $("drawer").setAttribute("data-open", kind);
  const titles = { claims: "Claims & Tasks", graph: "Call Graph",
    health: "LLM Health", feed: "Trace Feed", loops: "Loops & Limits" };
  $("drawer-title").textContent = titles[kind] || kind;
  $("drawer-content").innerHTML = `<div class="muted">loading…</div>`;
  ({ claims: dClaims, graph: dGraph, health: dHealth, feed: dFeed,
     loops: dLoops })[kind]();
}
function closeDrawer() {
  $("drawer").classList.add("hidden");
  $("drawer").removeAttribute("data-open");
}
async function dClaims() {
  const box = $("drawer-content");
  box.innerHTML = "<h3>Claims</h3><div class='table' id='claims-list'></div>" +
                  "<h3>Task queue</h3><div class='table' id='tasks-list'></div>";
  try {
    const c = await getJSON("/api/claims");
    state.claims = c.claims || [];
    $("claims-list").innerHTML =
      `<div class="feedline"><b>id</b><b>truth</b><b>func</b><b>claim</b></div>` +
      state.claims.slice(0, 200).map(x =>
        `<div class="feedline"><span>${x.id}</span>
         <span class="${x.truth_level==='high_confidence'?'st-ok':'tlevel'}">${esc(x.truth_level||"NULL")}</span>
         <span class="addr">${esc(x.function)}</span>
         <span>${esc((x.claim||"").slice(0,160))}</span></div>`).join("");
  } catch (e) { $("claims-list").innerHTML = `<span class="st-err">${esc(e)}</span>`; }
  try {
    const t = await getJSON("/api/tasks");
    state.tasks = t.tasks || [];
    $("tasks-list").innerHTML =
      `<div class="feedline"><b>id</b><b>status</b><b>rej</b><b>desc</b></div>` +
      state.tasks.slice(0, 200).map(x =>
        `<div class="feedline"><span>${x.id}</span>
         <span class="tlevel">${esc(x.status)}</span><span>${x.rejection_count||0}</span>
         <span>${esc((x.description||"").slice(0,120))}</span></div>`).join("");
  } catch (e) { $("tasks-list").innerHTML += `<span class="st-err">${esc(e)}</span>`; }
}
async function refreshClaims() { if ($("drawer").getAttribute("data-open")==="claims") dClaims(); }

async function dLoops() {
  const box = $("drawer-content");
  box.innerHTML = "<h3>Loops & Limits — non-termination watch</h3>" +
      "<div id='loops-box' class='pad muted'>loading…</div>";
  try {
    const l = await getJSON("/api/loops");
    state.loops = l;
    const lim = l.limits || {}, cnt = l.counters || {};
    const ctrs = [["LLM calls", cnt["llm.calls"]], ["LLM errors", cnt["llm.errors"]],
      ["LLM in-flight (incl queued)", cnt["llm.pending_in_flight"]],
      ["Critic rejections", cnt["claim.rejected"]],
      ["Pass2 review retries", cnt["pass2.review_retries"]],
      ["Resynthesis iterations", cnt["resynthesis.iterations"]],
      ["Investigation stuck", cnt["investigation.stuck"]],
      ["Scheduler reviewed", cnt["scheduler.reviewed"]]];
    const rows = ctrs.map(([k, v]) =>
      `<div class="barlabel"><span>${k}</span><b>${v}</b></div>
       <div class="bar"><i style="width:${Math.min(100, (v||0)/Math.max(1,(lim.max_investigation_iterations||100)/10)*10)}%"></i></div>`).join("");
    const lims = Object.entries(lim).map(([k, v]) =>
      `<div class="barlabel"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
    box.innerHTML = `
      <div class="grid2">
        <div><h3>live counters</h3>${rows}</div>
        <div><h3>configured caps (configs/default.toml [limits])</h3>${lims}</div>
      </div>
      <p class="muted">Every cap is config-driven and justified in docs/skills/telemetry.md —
      nothing here is imposed arbitrarily; these exist only to prevent genuinely
      runaway loops (livelock), and each is surfaced live so you can tune with evidence.</p>`;
  } catch (e) { $("loops-box").innerHTML = esc(e); }
}
async function refreshLoops() { if ($("drawer").getAttribute("data-open")==="loops") dLoops(); }

function dFeed() {
  $("drawer-content").innerHTML = `
    <div class="filterbar"><input id="feed-type" placeholder="filter type">
      <input id="feed-query" placeholder="filter text">
      <label><input type="checkbox" id="feed-follow" checked> follow</label></div>
    <div id="feed-list" class="table" style="max-height:70vh"></div>`;
  renderFeed();
  $("feed-type").oninput = renderFeed;
  $("feed-query").oninput = renderFeed;
}
async function dHealth() {
  const box = $("drawer-content");
  const h = state.health;
  box.innerHTML = `<h3>LLM Health</h3>
    <div class="kv">
      <b>completed calls</b><span>${h.calls}</span>
      <b>errors</b><span class="${h.errors?'st-err':'st-ok'}">${h.errors}</span>
      <b>avg duration</b><span>${h.calls ? (h.durSum/h.calls).toFixed(1)+"s" : "—"}</span>
      <b>max duration</b><span>${h.maxDur?s+h.maxDur.toFixed(1)+"s":"—"}</span>
      <b>requests &gt; 120s</b><span>${h.slowest.length}</span>
    </div>
    <h3>slow calls</h3><div id="slow-list"></div>
    <h3>recent errors</h3><div id="err-list"></div>`;
  $("slow-list").innerHTML = h.slowest.map(x =>
    `<div class="feedline"><span class="addr">${esc(x.session)}</span>
     <span>${x.dur.toFixed(1)}s</span><span class="tlevel">${esc(x.level)}</span>
     <span class="muted">${esc(x.ts)}</span></div>`).join("") || `<span class="muted">none</span>`;
  $("err-list").innerHTML = h.errList.map(x =>
    `<div class="feedline"><span class="addr">${esc(x.session)}</span>
     <span class="st-err">${esc(x.error)}</span><span class="muted">${esc(x.ts)}</span></div>`).join("")
    || `<span class="muted">none</span>`;
}
async function dGraph() {
  const box = $("drawer-content");
  box.innerHTML = `<canvas id="graph"></canvas>
    <p class="muted">CALL edges only · color: blue=pending, green=renamed, muted=pinned</p>`;
  try {
    const f = await getJSON("/api/functions");
    const e = await getJSON("/api/edges");
    const funcs = (f.functions || []).map(x => x.address);
    const nodes = new Map((f.functions || []).map(x => [x.address, x]));
    const edges = (e.edges || []).filter(x => x.type === "CALL" &&
      nodes.has(x.src) && nodes.has(x.dst));
    drawGraph(box, nodes, edges);
  } catch (err) { box.innerHTML += `<span class="st-err">${esc(err)}</span>`; }
}
async function drawGraph(box, nodes, edges) {
  const canvas = $("graph");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth || 640;
  const H = canvas.height = canvas.clientHeight || 420;
  const list = [...nodes.values()];
  const pos = new Map(list.map((n, i) => {
    const a = (i / Math.max(1, list.length)) * Math.PI * 2;
    return [n.address, { x: W/2 + Math.cos(a) * (H/2 - 40), y: H/2 + Math.sin(a) * (H/2 - 40),
                        vx: 0, vy: 0 }];
  }));
  const R = 5, K = 0.06, REP = 900;
  for (let iter = 0; iter < 220; iter++) {
    for (const n of list) {
      const p = pos.get(n.address);
      for (const m of list) {
        if (n === m) continue;
        const q = pos.get(m.address);
        const dx = p.x - q.x, dy = p.y - q.y;
        const d2 = dx*dx + dy*dy + 1e-6;
        p.vx += dx/Math.sqrt(d2) * REP/d2;
        p.vy += dy/Math.sqrt(d2) * REP/d2;
      }
    }
    for (const [src, dst] of edges.map(x => [x.src, x.dst])) {
      const p = pos.get(src), q = pos.get(dst);
      if (!p || !q) continue;
      p.vx += (q.x - p.x) * K; p.vy += (q.y - p.y) * K;
      q.vx += (p.x - q.x) * K; q.vy += (p.y - q.y) * K;
    }
    for (const n of list) {
      const p = pos.get(n.address);
      p.x += p.vx; p.y += p.vy; p.vx *= 0.82; p.vy *= 0.82;
      p.x = Math.max(R, Math.min(W - R, p.x));
      p.y = Math.max(R, Math.min(H - R, p.y));
    }
  }
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "#263041";
  for (const [src, dst] of edges.map(x => [x.src, x.dst])) {
    const p = pos.get(src), q = pos.get(dst);
    if (!p || !q) continue;
    ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke();
  }
  for (const n of list) {
    const p = pos.get(n.address);
    const col = n.pinned ? "#7d8aa0" : (n.canon_name || n.llm_name) ? "#3fb68b" : "#3fb6ff";
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(p.x, p.y, R, 0, Math.PI*2); ctx.fill();
  }
}

// ---- run control -----------------------------------------------------------
async function startRun() {
  const payload = { run_id: $("ctl-runid").value.trim(),
    binary: $("ctl-binary").value.trim(),
    phases: $("ctl-phases").value.trim() };
  try {
    const r = await fetch("/api/run/start", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });
    const j = await r.json();
    if (!j.ok) alert(j.error || "failed to start");
  } catch (e) { alert(e); }
}
async function stopRun() {
  try { await fetch("/api/run/stop", { method: "POST" }); } catch (e) {}
}
function setStatus(s) { const el = $("status"); el.textContent = s; el.className = `badge ${s}`; }

// ---- init ------------------------------------------------------------------
async function bootstrap() {
  try {
    const st = await getJSON("/api/state");
    state.run = st.run || null;
    if (st.files) state.files = Object.values(st.files).join("  ·  ");
    $("files").textContent = state.files.slice(0, 220);
    if (st.run && st.run.run_id) $("ctl-runid").value = st.run.run_id;
    if (state.run) {
      setStatus(state.run.status || "idle");
      renderPhases(); renderCoverage();
    }
  } catch (e) {}
  refreshFunctions();
  setInterval(refreshFunctions, 5000);   // renames are graph-side; poll for deltas
  setInterval(() => { if ($("drawer").getAttribute("data-open") === "claims") refreshClaims(); }, 8000);
  setInterval(() => { if ($("drawer").getAttribute("data-open") === "loops") refreshLoops(); }, 5000);
}

$("btn-start").onclick = startRun;
$("btn-stop").onclick = stopRun;
$("fn-filter").oninput = renderBoard;
$("dd-pause").onclick = () => { state.ddPaused = !state.ddPaused;
  $("dd-pause").textContent = state.ddPaused ? "Resume" : "Pause"; };
$("dd-clear").onclick = () => { state.sessions.clear(); renderDialogues(); };
$("drawer-close").onclick = closeDrawer;
document.querySelectorAll("#drawer-buttons button").forEach(b =>
  b.onclick = () => openDrawer(b.dataset.drawer));

bootstrap();
connectWS();
