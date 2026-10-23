// MitraFSnap demo dashboard. Talks to the backend only through its HTTP API,
// so the backend can run on this machine or anywhere else.
"use strict";

const ACTS = [
  { id: "prepare", n: "Setup", title: "Prepare", what: "models, clip, base images" },
  { id: "build-v1", n: "Act 1", title: "Build v1", what: "build and push the image" },
  { id: "deploy", n: "Act 2", title: "Deploy", what: "camera needs the detector only" },
  { id: "enable", n: "Act 3", title: "Enable segmenter", what: "a model the camera didn't fetch" },
  { id: "build-v2", n: "Act 4", title: "Build v2", what: "new detector + classes" },
  { id: "update", n: "Act 5", title: "Roll out v2", what: "replace vs refresh in place" },
  { id: "patch", n: "Act 6", title: "Patch unused model", what: "v3: build + roll out" },
];
const COLORS = { docker: "--docker", estargz: "--estargz", twodfs: "--twodfs", mitrafsnap: "--mitrafsnap" };
const LETTER = { docker: "D", estargz: "E", twodfs: "2", mitrafsnap: "M" };
const TAGS = {
  docker: ["eager pull", "replace"],
  estargz: ["lazy pull", "replace"],
  twodfs: ["partial eager", "replace"],
  mitrafsnap: ["partial lazy", "refresh in place"],
};

// Workflow diagram: nodes and edges (viewBox 1300 x 250).
const NODES = {
  build: [200, 45, "Build", "builder"], push: [340, 45, "Push", ""],
  registry: [480, 45, "Registry", "2dfs-registry"], pull: [620, 45, "Pull", "eager / lazy"],
  start: [760, 45, "Start", "container"], load: [900, 45, "Load models", "app opens files"],
  r_stop: [620, 135, "Stop", "old container"], r_pull: [760, 135, "Pull v2", ""],
  r_start: [900, 135, "Start", "new container"], r_load: [1040, 135, "Load models", "cold"],
  f_refresh: [760, 215, "Refresh mount", "TOC diff, swap readers"],
  f_reopen: [900, 215, "Reopen files", "changed models only"],
  serve: [1220, 135, "Serving", "frames out"],
};
const ROWS = [[45, "DELIVER"], [135, "UPDATE BY", "REPLACEMENT"], [215, "UPDATE", "IN PLACE"]];
const EDGES = [["build", "push"], ["push", "registry"], ["registry", "pull"], ["pull", "start"],
  ["start", "load"], ["load", "serve"], ["registry", "r_stop"], ["r_stop", "r_pull"],
  ["r_pull", "r_start"], ["r_start", "r_load"], ["r_load", "serve"], ["registry", "f_refresh"],
  ["f_refresh", "f_reopen"], ["f_reopen", "serve"]];

const $ = (s) => document.querySelector(s);
const state = {
  base: "", token: "", info: null, es: null,
  lanes: {},           // key -> lane state
  acts: {},            // act -> {state, t}
  running: null,
  clock: 0,            // server seconds at last event
  clockAt: 0,          // performance.now() at last event
};

function now() { return state.clock + (performance.now() - state.clockAt) / 1000; }
function url(path) {
  const sep = path.includes("?") ? "&" : "?";
  return state.base + path + (state.token ? `${sep}token=${encodeURIComponent(state.token)}` : "");
}
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } };
  const r = await fetch(url(path), opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
function mb(bytes) {
  if (bytes == null) return "–";
  if (bytes < 1e6) return `${(bytes / 1e3).toFixed(0)} KB`;
  if (bytes < 1e9) return `${(bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0)} MB`;
  return `${(bytes / 1e9).toFixed(2)} GB`;
}
function secs(s) { return s == null ? "–" : s < 10 ? `${s.toFixed(1)} s` : `${s.toFixed(0)} s`; }
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function newLane(key) {
  return {
    key, node: null, phase: "idle", frames: [], version: null, models: [], bytes: 0,
    build: {}, push: {}, deployAt: null, firstFrame: null, enableAt: null, segReady: null,
    updateAt: null, v2At: null, refresh: null,
    patchAt: null, v3At: null, patchBytes0: null, patchBytes: null,
  };
}

// ---------------------------------------------------------------- connection

function loadPrefs() {
  const q = new URLSearchParams(location.search);
  let base = q.get("backend") ?? localStorage.getItem("backend") ?? "";
  if (!base && location.protocol === "file:") base = "http://localhost:8080";
  state.base = base.replace(/\/$/, "");
  state.token = q.get("token") ?? localStorage.getItem("token") ?? "";
  $("#backend").value = state.base;
  $("#token").value = state.token;
}

async function connect() {
  state.base = $("#backend").value.trim().replace(/\/$/, "");
  state.token = $("#token").value.trim();
  try { localStorage.setItem("backend", state.base); localStorage.setItem("token", state.token); }
  catch (e) { /* storage may be unavailable */ }
  if (state.es) state.es.close();
  setStatus("connecting", "");
  try {
    state.info = await api("/api/info");
  } catch (e) {
    setStatus(`offline: ${e.message}`, "bad");
    return;
  }
  for (const m of state.info.methods) state.lanes[m.key] ??= newLane(m.key);
  for (const a of state.info.done) state.acts[a] = { state: "end" };
  state.running = state.info.running;
  if (state.running) state.acts[state.running] = { state: "start" };
  renderControls();
  renderLanes();
  renderActs();
  // Catch up on the current run, so a page opened mid-demo shows it all.
  try {
    const trace = await api("/api/trace");
    for (const ev of trace.events) if (ev.kind !== "sample") onEvent(ev);
    for (const ev of trace.events.slice(-1)) if (ev.kind === "sample") onEvent({ ...ev, kind: "stats" });
  } catch (e) { /* older backend without a trace: start empty */ }
  state.es = new EventSource(url("/api/events"));
  state.es.onopen = () => setStatus(`${state.info.backend.arch} · ${state.info.backend.cpus} CPUs`, "ok");
  state.es.onerror = () => setStatus("reconnecting", "bad");
  state.es.onmessage = (m) => onEvent(JSON.parse(m.data));
}

function setStatus(text, cls) {
  const el = $("#status");
  el.textContent = text;
  el.className = `pill ${cls}`;
}

// ---------------------------------------------------------------- controls

function renderControls() {
  const { info } = state;
  $("#scenario").innerHTML = info.scenarios.map((s) =>
    `<option ${s === info.settings.scenario ? "selected" : ""}>${s}</option>`).join("");
  $("#scenario-desc").textContent = info.scenario.description;
  $("#link").value = String(info.settings.link_mbit || 0);
  $("#lane-toggles").innerHTML = info.methods.map((m) => `
    <label style="border-color:var(${COLORS[m.key]})">
      <input type="checkbox" value="${m.key}" ${info.settings.lanes.includes(m.key) ? "checked" : ""}>
      ${m.title}</label>`).join("");
  $("#lane-toggles").onchange = () => setLanes(
    [...document.querySelectorAll("#lane-toggles input:checked")].map((i) => i.value));
  const credits = Object.values(info.scenario.credits)
    .map((c) => `${c.credit} (${c.license})`).join(" · ");
  $("#credits").textContent = `Assets: ${credits}`;
}

async function setLanes(lanes) {
  try {
    const s = await api("/api/settings", { lanes });
    state.info.settings = s;
  } catch (e) {
    log(`settings: ${e.message}`);
  }
  renderControls();
  renderLanes();
}

function renderActs() {
  const doneAll = (id) => state.acts[id]?.state === "end";
  const next = ACTS.find((a) => !doneAll(a.id));
  const html = ACTS.map((a) => {
    const st = state.acts[a.id]?.state;
    const cls = st === "start" ? "running" : st === "end" ? "done" : st === "error" ? "error" : "";
    return `<button class="act ${cls} ${next && next.id === a.id && !state.running ? "next" : ""}"
      data-act="${a.id}" ${state.running ? "disabled" : ""}>
      <span class="n">${a.n}</span><strong>${a.title}</strong><span class="what">${a.what}</span>
    </button>`;
  }).join("") + `<button class="act reset" data-act="reset" ${state.running ? "disabled" : ""}>Reset</button>`;
  $("#acts").innerHTML = html;
  for (const b of document.querySelectorAll("#acts button")) {
    b.onclick = () => runAct(b.dataset.act);
  }
}

async function runAct(act) {
  try {
    await api(`/api/acts/${act}`, {});
  } catch (e) {
    log(`${act}: ${e.message}`);
  }
}

// ---------------------------------------------------------------- lanes

function renderLanes() {
  const shown = state.info.settings.lanes;
  $("#lanes").innerHTML = shown.map((k) => {
    const m = state.info.methods.find((x) => x.key === k);
    return `<article class="lane" id="lane-${k}">
      <header>
        <span class="swatch" style="background:var(${COLORS[k]})"></span>
        <h2>${m.title}</h2>
        <span class="tags">${TAGS[k].map((t) => `<span class="pill">${t}</span>`).join("")}</span>
        <span class="phase" data-f="phase">idle</span>
      </header>
      <div class="video"><img alt="${m.title} output" src="${url(`/api/lanes/${k}/video`)}">
        <span class="badge" data-f="badge"></span></div>
      <canvas class="timeline" data-f="timeline" width="760" height="34"></canvas>
      <div class="metrics">
        ${metric("build", "Build + push")}${metric("down", "Downloaded")}${metric("first", "First frame after deploy")}
        ${metric("seg", "Segmenter ready")}${metric("v2", "v2 live after")}${metric("gap", "Blind during v2 rollout")}
        ${metric("pbuild", "v3 build + push")}${metric("pdown", "v3 rollout downloaded")}${metric("pgap", "Blind during v3 rollout")}
      </div>
    </article>`;
  }).join("");
  drawDiagram();
}

function metric(id, label) {
  return `<div class="metric"><div class="k">${label}</div><div class="v" data-m="${id}">–</div></div>`;
}

function laneEl(k, sel) { return document.querySelector(`#lane-${k} ${sel}`); }

function updateLane(k) {
  const L = state.lanes[k];
  if (!document.getElementById(`lane-${k}`)) return;
  laneEl(k, "[data-f=phase]").textContent = L.phase;
  const fps = L.frames.filter((t) => now() - t < 2).length / 2;
  laneEl(k, "[data-f=badge]").textContent = L.version ? `${L.version} · ${fps.toFixed(1)} fps` : "";
  const set = (id, html, cls = "") => {
    const el = laneEl(k, `[data-m=${id}]`);
    el.innerHTML = html;
    el.className = `v ${cls}`;
  };
  const b = (v) => (L.build[v] != null ? L.build[v] + (L.push[v] || 0) : null);
  set("build", L.build.v2 != null
    ? `${secs(b("v2"))} <small>v1 ${secs(b("v1"))}</small>` : secs(b("v1")));
  set("down", mb(L.bytes));
  set("first", L.firstFrame != null ? secs(L.firstFrame) : L.deployAt != null ? "…" : "–");
  set("seg", L.segReady != null ? secs(L.segReady) : L.enableAt != null ? "…" : "–");
  if (L.v2At != null) {
    set("v2", secs(L.v2At), "");
    const gap = blindTime(L);
    set("gap", gap < 0.5 ? "0 s" : secs(gap), gap < 0.5 ? "good" : "bad");
  } else {
    set("v2", L.updateAt != null ? "…" : "–");
    set("gap", L.updateAt != null ? secs(blindTime(L)) : "–", L.updateAt != null ? "bad" : "");
  }
  set("pbuild", secs(b("v3")));
  if (L.patchAt != null) {
    const done = L.v3At != null;
    const pgap = blindTime(L, L.patchAt, done ? L.patchAt + L.v3At : null);
    set("pdown", mb((L.patchBytes ?? L.bytes) - L.patchBytes0));
    set("pgap", done && pgap < 0.5 ? "0 s" : secs(pgap), done && pgap < 0.5 ? "good" : "bad");
  } else {
    set("pdown", "–");
    set("pgap", "–");
  }
}

// Seconds without output between the start of a rollout and the new version
// going live (or now, while it hasn't).
function blindTime(L, start = L.updateAt, end = L.v2At != null ? L.updateAt + L.v2At : null) {
  const until = end ?? now();
  let blind = 0, last = start;
  for (const t of L.frames) {
    if (t <= start) continue;
    if (t > until) break;
    if (t - last > 0.5) blind += t - last;
    last = t;
  }
  if (end == null && until - last > 0.5) blind += until - last;
  return blind;
}

function drawTimeline(k) {
  const c = laneEl(k, "[data-f=timeline]");
  if (!c) return;
  const L = state.lanes[k];
  const w = c.width, h = c.height, span = 90, t1 = now(), t0 = t1 - span;
  const g = c.getContext("2d");
  g.clearRect(0, 0, w, h);
  const x = (t) => ((t - t0) / span) * w;
  const deployed = L.deployAt != null ? L.deployAt : Infinity;
  // red where the lane should have been serving but produced nothing
  g.fillStyle = "rgba(239,107,107,.35)";
  let last = Math.max(t0, deployed);
  for (const t of L.frames.concat([t1])) {
    if (t < t0) { last = Math.max(last, t); continue; }
    if (t - last > 0.5 && last >= deployed) g.fillRect(x(last), 0, x(t) - x(last), h);
    last = t;
  }
  g.fillStyle = cssVar(COLORS[k]);
  for (const t of L.frames) if (t >= t0) g.fillRect(x(t), 8, 1.5, h - 16);
  g.fillStyle = "rgba(230,232,238,.55)";
  g.font = "10px system-ui";
  for (const [act, a] of Object.entries(state.acts)) {
    if (a.t == null || a.t < t0) continue;
    g.fillRect(x(a.t), 0, 1, h);
    g.fillText(ACTS.find((z) => z.id === act)?.n ?? act, x(a.t) + 3, 10);
  }
}

// ---------------------------------------------------------------- diagram

function drawDiagram() {
  const svg = $("#diagram");
  const shown = state.info?.settings.lanes ?? [];
  const edge = ([a, b]) => {
    const [x1, y1] = NODES[a], [x2, y2] = NODES[b];
    const sx = x1 + 58, ex = x2 - 58;
    return `<path class="edge" d="M${sx},${y1} C${(sx + ex) / 2},${y1} ${(sx + ex) / 2},${y2} ${ex},${y2}"/>`;
  };
  const node = ([id, [x, y, label, hint]]) => `<g class="node" data-node="${id}">
      <rect x="${x - 58}" y="${y - 22}" width="116" height="44" rx="8"/>
      <text x="${x}" y="${y - 2}" text-anchor="middle">${label}</text>
      <text class="hint" x="${x}" y="${y + 13}" text-anchor="middle">${hint}</text></g>`;
  const row = ([y, a, b]) => `<text class="rowlabel" x="12" y="${b ? y - 3 : y + 4}">${a}</text>` +
    (b ? `<text class="rowlabel" x="12" y="${y + 11}">${b}</text>` : "");
  svg.innerHTML = `
    ${ROWS.map(row).join("")}
    ${EDGES.map(edge).join("")}${Object.entries(NODES).map(node).join("")}
    ${shown.map((k, i) => `<g class="marker" id="marker-${k}" style="opacity:0">
      <circle r="9" fill="var(${COLORS[k]})"/><text text-anchor="middle" y="3.5">${LETTER[k]}</text>
    </g>`).join("")}`;
  for (const k of shown) placeMarker(k);
}

function placeMarker(k) {
  const el = document.getElementById(`marker-${k}`);
  if (!el) return;
  const L = state.lanes[k];
  if (!L.node) { el.style.opacity = 0; return; }
  const shown = state.info.settings.lanes;
  const i = shown.indexOf(k);
  const [x, y] = NODES[L.node];
  el.style.opacity = 1;
  el.style.transform = `translate(${x - 44 + i * 22}px, ${y + 30}px)`;
}

function setNode(k, node, phase) {
  const L = state.lanes[k];
  L.node = node;
  if (phase) L.phase = phase;
  placeMarker(k);
}

// ---------------------------------------------------------------- events

function onEvent(ev) {
  if (ev.t != null) { state.clock = ev.t; state.clockAt = performance.now(); }
  const L = ev.lane ? state.lanes[ev.lane] : null;
  switch (ev.kind) {
    case "stats":
      for (const [k, s] of Object.entries(ev.lanes)) if (state.lanes[k]) state.lanes[k].bytes = s.bytes;
      return;
    case "frame":
      onFrame(L, ev);
      return;
    case "act":
      state.acts[ev.act] = { state: ev.state, t: ev.state === "start" ? ev.t : state.acts[ev.act]?.t };
      state.running = ev.state === "start" ? ev.act : null;
      if (ev.state === "start") onActStart(ev);
      log(`${ev.act} ${ev.state}${ev.seconds != null ? ` in ${secs(ev.seconds)}` : ""}${ev.error ? `: ${ev.error}` : ""}`);
      renderActs();
      return;
    case "phase":
      onPhase(L, ev);
      break;
    case "app":
      onApp(L, ev);
      break;
    case "reset":
      for (const k of Object.keys(state.lanes)) state.lanes[k] = newLane(k);
      state.acts = { prepare: { state: "end" } };
      renderActs();
      drawDiagram();
      log("reset");
      break;
    case "settings":
      state.info.settings = ev.settings;
      renderControls();
      renderLanes();
      break;
    case "prepare":
      if (ev.step === "download") {
        if (!ev.done || ev.done === ev.total) log(`download ${ev.asset} ${mb(ev.total)}`);
      } else log(`prepare: ${ev.step}`);
      break;
    case "rollout":
      if (ev.version === "v3") { L.patchAt = ev.t; L.v3At = null; L.patchBytes0 = L.bytes; L.patchBytes = null; }
      break;
    case "ready":
      if (ev.what === "v3 live") { L.v3At = ev.t - L.patchAt; L.patchBytes = L.bytes; }
      break;
    case "redeploy":
      log(`${ev.lane}: redeploy needed (${ev.reason})`);
      break;
    case "lane_error":
      L.phase = "error";
      log(`${ev.lane}: ${ev.error}`);
      break;
  }
  if (ev.lane) updateLane(ev.lane);
}

function onActStart(ev) {
  for (const k of ev.lanes || []) {
    const L = state.lanes[k];
    if (ev.act === "deploy") { L.deployAt = ev.t; L.firstFrame = null; }
    if (ev.act === "enable") { L.enableAt = ev.t; L.segReady = null; }
    if (ev.act === "update") { L.updateAt = ev.t; L.v2At = null; }
  }
}

function onPhase(L, ev) {
  const k = L.key;
  const updating = state.running === "update";
  const label = { build: "building", push: "pushing", pull: "pulling", start: "starting",
    stop: "stopping", refresh: "refreshing", enable: "loading model" }[ev.phase] || ev.phase;
  if (ev.state === "start") {
    const node = {
      build: "build", push: "push", pull: updating ? "r_pull" : "pull",
      start: updating ? "r_start" : "start", stop: "r_stop", refresh: "f_refresh",
      enable: "load",
    }[ev.phase];
    setNode(k, node, label);
  } else if (ev.state === "end") {
    if (ev.phase === "build" || ev.phase === "push") {
      L[ev.phase][ev.version] = ev.seconds;
      if (ev.phase === "push") setNode(k, recentlyServing(L) ? "serve" : "registry",
        recentlyServing(L) ? "serving" : "published");
    }
    if (ev.phase === "refresh") L.refresh = ev;
    log(`${k}: ${ev.phase} ${secs(ev.seconds)}${ev.bytes ? `, ${mb(ev.bytes)} over the link` : ""}`);
  } else if (ev.state === "error") {
    L.phase = `${ev.phase} failed`;
    log(`${k}: ${ev.phase} failed: ${ev.error}`);
  }
}

function onApp(L, ev) {
  const k = L.key;
  const updating = state.running === "update";
  if (ev.phase === "load_start") setNode(k, updating ? (L.key === "mitrafsnap" ? "f_reopen" : "r_load") : "load", `loading ${ev.model}`);
  if (ev.phase === "reload_start") setNode(k, "f_reopen", "reopening files");
  if (ev.phase === "missing") log(`${k}: ${ev.model} is not in this container's image`);
  if (ev.phase === "load_done") log(`${k}: loaded ${ev.model} in ${secs(ev.seconds)}`);
  if (ev.phase === "reload_done") log(`${k}: reloaded ${ev.models.join(", ") || "nothing"} in ${secs(ev.seconds)}`);
}

function recentlyServing(L) { const t = L.frames[L.frames.length - 1]; return t && now() - t < 1.5; }

function onFrame(L, ev) {
  L.frames.push(ev.t);
  if (L.frames.length > 4000) L.frames.splice(0, 1000);
  L.version = ev.version;
  L.models = ev.models || [];
  if (L.deployAt != null && L.firstFrame == null) L.firstFrame = ev.t - L.deployAt;
  if (L.enableAt != null && L.segReady == null && L.models.includes("segmenter")) L.segReady = ev.t - L.enableAt;
  if (L.updateAt != null && L.v2At == null && ev.version === "v2") L.v2At = ev.t - L.updateAt;
  if (!["build", "push"].includes(L.node) && L.node !== "serve") setNode(L.key, "serve", "serving");
  else if (L.node === "serve") L.phase = "serving";
}

function log(text) {
  const li = document.createElement("li");
  li.innerHTML = `${now().toFixed(1).padStart(7)}  <span></span>`;
  li.querySelector("span").textContent = text;
  const ol = $("#log");
  ol.prepend(li);
  while (ol.children.length > 300) ol.lastChild.remove();
}

// ---------------------------------------------------------------- loop

function tick() {
  if (state.info) for (const k of state.info.settings.lanes) { updateLane(k); drawTimeline(k); }
  requestAnimationFrame(tick);
}

$("#connect").onclick = connect;
$("#all-lanes").onclick = () => setLanes(state.info.methods.map((m) => m.key));
$("#link").onchange = async (e) => {
  try { await api("/api/settings", { link_mbit: Number(e.target.value) }); } catch (err) { log(err.message); }
};
$("#scenario").onchange = async (e) => {
  try { await api("/api/settings", { scenario: e.target.value }); await connect(); } catch (err) { log(err.message); }
};
loadPrefs();
connect();
requestAnimationFrame(tick);
