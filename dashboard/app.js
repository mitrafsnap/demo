// MitraFSnap demo dashboard. Talks to the backend only through its HTTP API,
// so the backend can run on this machine or anywhere else.
"use strict";

// The four acts. Every update act builds, pushes and rolls out one version.
const ACTS = [
  { id: "deploy", n: "Act 1", title: "Deploy v1", what: "build, push and start the camera" },
  { id: "add-model", n: "Act 2", title: "Add a model", what: "v2: build, push, roll out" },
  { id: "upgrade", n: "Act 3", title: "Upgrade a model", what: "v3: build, push, roll out" },
  { id: "patch", n: "Act 4", title: "Patch unused model", what: "v4: build, push, roll out" },
];
const UPDATES = [
  { act: "add-model", version: "v2", n: "Act 2" },
  { act: "upgrade", version: "v3", n: "Act 3" },
  { act: "patch", version: "v4", n: "Act 4" },
];
const COLORS = { docker: "--docker", estargz: "--estargz", twodfs: "--twodfs", mitrafsnap: "--mitrafsnap" };
const LETTER = { docker: "D", estargz: "E", twodfs: "2", mitrafsnap: "M" };
const TAGS = {
  docker: ["eager pull", "replace"],
  estargz: ["lazy pull", "replace"],
  twodfs: ["partial eager", "replace"],
  mitrafsnap: ["partial lazy", "refresh in place"],
};
const DEVICE_NAME = { cuda: "NVIDIA GPU", apple: "Apple GPU", cpu: "CPU" };

// Captions, from the scenario's own facts (scenario.json "words").
function narration(id) {
  const w = state.info?.scenario.words || {};
  return {
    deploy: `Every method builds v1, pushes it and starts the camera, which needs only ${w.needs}. ` +
      "Docker and 2DFS download their image first (2DFS only the selected parts); eStargz and " +
      "MitraFSnap start right away and fetch files as the app reads them.",
    "add-model": `v2 adds ${w.added}. Every method rebuilds and pushes. Docker, eStargz and 2DFS then ` +
      "stop the camera, pull v2 and start a new container; MitraFSnap updates the files of the " +
      "running container, and the app loads the model while it keeps serving.",
    upgrade: `v3 ships ${w.v3}. Docker and eStargz rebuild every layer after the changed one; 2DFS ` +
      "and MitraFSnap rebuild only the changed part. Replacement restarts the camera again; " +
      `MitraFSnap swaps the model in place, and ${w.visible_v3}.`,
    patch: "v4 replaces a 528 MB model this camera never loads. Docker downloads it again and every " +
      "replacement method restarts the camera; MitraFSnap's refresh fetches only the new file index " +
      "(a few KB) and the camera never stops.",
    reset: "Clearing every lane, image and cache, so the next run starts cold.",
    prepare: "Downloading the models and the clip, and building the base images.",
  }[id];
}

// Workflow diagram: nodes and edges (viewBox 1300 x 262).
const NODES = {
  build: [200, 45, "Build", "builder"], push: [340, 45, "Push", ""],
  registry: [480, 45, "Registry", "2dfs-registry"], pull: [620, 45, "Pull", "eager / lazy"],
  start: [760, 45, "Start", "container"], load: [900, 45, "Load models", "app opens files"],
  r_stop: [620, 135, "Stop", "old container"], r_pull: [760, 135, "Pull new", ""],
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
const LINK_OPTIONS = document.querySelector("#link").innerHTML;
const state = {
  base: "", token: "", info: null, es: null,
  lanes: {},           // key -> lane state
  acts: {},            // act -> {state, t}
  running: null,
  clock: 0,            // server seconds at last event
  clockAt: 0,          // performance.now() at last event
  tables: false,       // charts shown as tables
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
function secs(s) {
  if (s == null) return "–";
  return s === 0 ? "0 s" : s < 10 ? `${s.toFixed(1)} s` : `${s.toFixed(0)} s`;
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
function shown() { return state.info?.settings.lanes ?? []; }
function title(k) { return state.info.methods.find((m) => m.key === k).title; }

function newLane(key) {
  return {
    key, node: null, phase: "idle", frames: [], version: null, models: [], bytes: 0,
    series: [],          // [t, bytes] over the run, for the chart
    build: {}, push: {}, rebuilt: {},   // per version: seconds, seconds, {rebuilt, of}
    firstFrameAt: null,
    live: {},            // version -> time its first frame was served
    rolling: false,      // a rollout is in progress on this lane
  };
}

// ---------------------------------------------------------------- measurements

// Every number on the page comes from here, from the same event times, so the
// lane metrics, the charts and the tables always agree.
const BLIND = 1.0;   // a gap shorter than this is a slow frame, not an outage

function gapsBetween(L, start, end) {
  let total = 0, last = start;
  for (const t of L.frames) {
    if (t <= start) continue;
    if (t > end + 0.05) break;   // the first new-version frame closes the window
    if (t - last > BLIND) total += t - last;
    last = t;
  }
  if (end - last > BLIND) total += end - last;
  return total;
}

function measure(L) {
  const act = (id) => state.acts[id]?.t;
  const m = { updates: {}, downtime: null, latest: null, total: null };
  if (L.firstFrameAt == null) return m;   // this lane hasn't served in this run
  if (act("deploy") != null) m.deploy = L.firstFrameAt - act("deploy");
  for (const u of UPDATES) {
    const start = act(u.act);
    if (start == null) continue;
    const live = L.live[u.version];
    const end = live ?? now();
    m.updates[u.version] = { toChange: live != null ? live - start : null,
      downtime: gapsBetween(L, start, end), done: live != null };
  }
  const ups = Object.values(m.updates);
  m.downtime = ups.length ? ups.reduce((a, u) => a + u.downtime, 0) : null;
  const doneUps = ups.filter((u) => u.done);
  m.latest = doneUps.length ? doneUps[doneUps.length - 1].toChange : null;
  m.total = m.deploy != null ? m.deploy + doneUps.reduce((a, u) => a + u.toChange, 0) : null;
  return m;
}

// Build and push time summed over every version built so far in this run.
function totalBuild(L) {
  const times = ["v1", "v2", "v3", "v4"].map((v) => buildTime(L, v)).filter((t) => t != null);
  return times.length ? times.reduce((a, b) => a + b, 0) : null;
}

function buildTime(L, v) { return L.build[v] != null ? L.build[v] + (L.push[v] || 0) : null; }

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
  if (state.es) { state.es.onerror = null; state.es.close(); }
  setStatus("connecting", "");
  try {
    state.info = await api("/api/info");
  } catch (e) {
    setStatus(`offline: ${e.message}`, "bad");
    setTimeout(connect, 3000);
    return;
  }
  for (const m of state.info.methods) state.lanes[m.key] = newLane(m.key);
  state.acts = {};
  state.lastAct = null;
  for (const a of state.info.done) state.acts[a] = { state: "end" };
  state.running = state.info.running;
  if (state.running) state.acts[state.running] = { state: "start" };
  renderControls();
  renderLanes();
  renderActs();
  // Catch up on the current run, so a page opened mid-demo shows it all.
  try {
    const trace = await api("/api/trace");
    for (const ev of trace.events) onEvent(ev.kind === "sample" ? { ...ev, kind: "stats" } : ev);
  } catch (e) { /* older backend without a trace: start empty */ }
  openVideo();
  const es = new EventSource(url("/api/events"));
  es.onopen = () => setStatus(statusText(), "ok");
  // The backend restarted or the network dropped: reload everything from the
  // trace instead of mixing two runs' clocks.
  es.onerror = () => { setStatus("reconnecting", "bad"); es.close(); setTimeout(connect, 1500); };
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  state.es = es;
  renderCharts();
}

// Webcam: frames from this browser's camera replace the scenario's fallback clip
// for every lane. Browsers only allow the camera on https or localhost.
async function toggleWebcam() {
  const btn = $("#webcam");
  if (state.cam) {
    clearInterval(state.cam.timer);
    state.cam.stream.getTracks().forEach((t) => t.stop());
    state.cam.ws.close();
    state.cam = null;
    btn.textContent = "Use my webcam";
    return;
  }
  let stream;
  try {
    if (!navigator.mediaDevices) throw new Error("the browser only allows the camera on https or localhost");
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 960, height: 540 } });
  } catch (e) {
    $("#narration").textContent = `Webcam unavailable: ${e.message}. Open the dashboard on ` +
      "localhost (an SSH tunnel works for a remote backend) or over https.";
    log(`webcam: ${e.message}`);
    return;
  }
  const video = Object.assign(document.createElement("video"), { srcObject: stream, muted: true });
  await video.play();
  const canvas = document.createElement("canvas");
  const u = new URL(url("/api/camera"), location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(u.href);
  const timer = setInterval(() => {
    if (ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 1 << 20) return;
    canvas.width = 960;
    canvas.height = Math.round(960 * video.videoHeight / video.videoWidth) || 540;
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((b) => b && ws.send(b), "image/jpeg", 0.8);
  }, 100);
  state.cam = { stream, ws, timer };
  btn.textContent = "Stop webcam";
}

// Lane frames arrive over one WebSocket: [lane index byte][JPEG].
function openVideo() {
  if (state.ws) { state.ws.onclose = null; state.ws.close(); }
  const u = new URL(url("/api/video"), location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(u.href);
  ws.binaryType = "arraybuffer";
  ws.onmessage = (m) => {
    const lane = state.info.methods[new Uint8Array(m.data, 0, 1)[0]]?.key;
    const img = lane && laneEl(lane, ".video img");
    if (!img) return;
    const blob = new Blob([new Uint8Array(m.data, 1)], { type: "image/jpeg" });
    const old = img.dataset.url;
    img.src = img.dataset.url = URL.createObjectURL(blob);
    if (old) URL.revokeObjectURL(old);
    state.lanes[lane].shownAt = performance.now();
  };
  ws.onclose = () => setTimeout(() => { if (state.ws === ws) openVideo(); }, 1000);
  state.ws = ws;
}

function effectiveDevice() {
  const { backend, settings } = state.info;
  if (settings.effective_device) return settings.effective_device;
  if (settings.device !== "auto") return settings.device;
  return backend.accelerators.cuda ? "cuda" : backend.accelerators.apple ? "apple" : "cpu";
}

function statusText() {
  const { backend } = state.info;
  return `${backend.arch} · ${backend.cpus} CPUs · ${DEVICE_NAME[effectiveDevice()]} inference`;
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
  // The link is only emulated when the registry runs on this machine.
  $("#link").innerHTML = info.link.emulated ? LINK_OPTIONS : "<option>real network</option>";
  $("#link").value = String(info.settings.link_mbit || 0);
  $("#link").disabled = !info.link.emulated;
  $("#link").title = info.link.emulated ? "bandwidth between the registry and the lanes"
    : `the registry is on another machine (${info.link.registry}); the real network is the link`;
  $("#registry").value = info.settings.registry || "";
  $("#builder").value = info.settings.builder || "";
  const acc = info.backend.accelerators;
  for (const [dev, label] of [["apple", "Apple GPU"], ["cuda", "NVIDIA GPU"]]) {
    const opt = $(`#device option[value=${dev}]`);
    opt.disabled = !acc[dev];
    opt.textContent = acc[dev] ? label : `${label} (not found)`;
  }
  $("#device").value = info.settings.device || "auto";
  $("#lane-toggles").innerHTML = info.methods.map((m) => `
    <label>
      <input type="checkbox" value="${m.key}" ${info.settings.lanes.includes(m.key) ? "checked" : ""}>
      <span class="dot" style="background:var(${COLORS[m.key]})"></span>${m.title}</label>`).join("");
  $("#lane-toggles").onchange = () => setLanes(
    [...document.querySelectorAll("#lane-toggles input:checked")].map((i) => i.value));
  $("#webcam").hidden = info.scenario.frames !== "camera";
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
  renderCharts();
}

function actInfo(id) {
  const base = ACTS.find((a) => a.id === id);
  if (!base) return null;
  const add = state.info?.scenario.add;
  return { ...base, title: id === "add-model" && add ? `Add ${add}` : base.title };
}

function renderActs() {
  const doneAll = (id) => state.acts[id]?.state === "end";
  const next = ACTS.find((a) => !doneAll(a.id));
  const html = ACTS.map(({ id }) => actInfo(id)).map((a) => {
    const st = state.acts[a.id]?.state;
    const cls = st === "start" ? "running" : st === "end" ? "done" : st === "error" ? "error" : "";
    return `<button class="act ${cls} ${next && next.id === a.id && !state.running ? "next" : ""}"
      data-act="${a.id}" ${state.running ? "disabled" : ""}>
      <span class="n">${a.n}</span><strong>${a.title}</strong><span class="what">${a.what}</span>
    </button>`;
  }).join("") + `<button class="act reset" data-act="reset" ${state.running ? "disabled" : ""}>Reset</button>`;
  $("#acts").innerHTML = html;
  const act = actInfo(state.lastAct);
  const text = narration(state.lastAct);
  $("#narration").innerHTML = text
    ? `<strong>${act ? `${act.n} · ${act.title}` : cap(state.lastAct)}</strong> ${text}` : "";
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
  const lanes = shown();
  $("#lanes").classList.toggle("one", lanes.length === 1);
  $("#lanes").innerHTML = lanes.map((k) => `<article class="lane" id="lane-${k}">
      <header>
        <span class="swatch" style="background:var(${COLORS[k]})"></span>
        <h2>${title(k)}</h2>
        <span class="tags">${TAGS[k].map((t) => `<span class="pill">${t}</span>`).join("")}</span>
        <span class="phase" data-f="phase">idle</span>
      </header>
      <div class="body">
      <div class="media">
        <div class="video"><img alt="${title(k)} output">
          <span class="nosignal" data-f="nosignal">no output</span>
          <span class="badge" data-f="badge"></span></div>
        <canvas class="timeline" data-f="timeline" width="760" height="26"></canvas>
      </div>
      <div class="metrics">
        ${metric("down", "Total downtime")}${metric("buildsum", "Total build + push")}
        ${metric("latest", "Latest change live after")}${metric("total", "Total time, all acts")}
        ${metric("bytes", "Downloaded")}${metric("build", "Latest build + push")}
      </div>
      </div>
    </article>`).join("");
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
  laneEl(k, "[data-f=badge]").textContent = L.version && fps ? `${L.version} · ${fps.toFixed(1)} fps` : "";
  const stale = !L.shownAt || performance.now() - L.shownAt > 1500;
  laneEl(k, ".video").classList.toggle("stale", stale);
  const set = (id, html, cls = "") => {
    const el = laneEl(k, `[data-m=${id}]`);
    el.innerHTML = html;
    el.className = `v ${cls}`;
  };
  const m = measure(L);
  const anyDone = Object.values(m.updates).some((u) => u.done);
  set("down", m.downtime == null ? "–" : secs(m.downtime),
    m.downtime == null ? "" : m.downtime === 0 && anyDone ? "good" : m.downtime > 0 ? "bad" : "");
  const parts = Object.values(L.rebuilt);
  const rebuilt = parts.reduce((a, r) => a + r.rebuilt, 0), of = parts.reduce((a, r) => a + r.of, 0);
  set("buildsum", totalBuild(L) == null ? "–" :
    `${secs(totalBuild(L))} <small>rebuilt ${rebuilt} of ${of} parts</small>`);
  set("latest", secs(m.latest ?? m.deploy));
  set("total", secs(m.total));
  set("bytes", mb(L.bytes));
  const v = ["v4", "v3", "v2", "v1"].find((x) => buildTime(L, x) != null);
  const r = v && L.rebuilt[v];
  set("build", v ? `${secs(buildTime(L, v))} <small>${v}${r ? `, rebuilt ${r.rebuilt} of ${r.of}` : ""}</small>` : "–");
}

function drawTimeline(k) {
  const c = laneEl(k, "[data-f=timeline]");
  if (!c) return;
  const px = Math.round(c.clientWidth * devicePixelRatio);
  if (px && c.width !== px) { c.width = px; c.height = Math.round(26 * devicePixelRatio); }
  const L = state.lanes[k];
  const w = c.width, h = c.height, span = 90, t1 = now(), t0 = t1 - span;
  const g = c.getContext("2d");
  g.clearRect(0, 0, w, h);
  const x = (t) => ((t - t0) / span) * w;
  const serving = L.firstFrameAt ?? Infinity;
  // red where the lane should have been serving but produced nothing
  g.fillStyle = "rgba(239,107,107,.35)";
  let last = Math.max(t0, serving);
  for (const t of L.frames.concat([t1])) {
    if (t < t0) { last = Math.max(last, t); continue; }
    if (t - last > BLIND && last >= serving) g.fillRect(x(last), 0, x(t) - x(last), h);
    last = t;
  }
  g.fillStyle = cssVar(COLORS[k]);
  for (const t of L.frames) if (t >= t0) g.fillRect(x(t), 8, 1.5, h - 16);
  g.fillStyle = "rgba(230,232,238,.6)";
  g.font = `${12 * devicePixelRatio}px ${cssVar("--font")}`;
  for (const a of ACTS) {
    const t = state.acts[a.id]?.t;
    if (t == null || t < t0) continue;
    g.fillRect(x(t), 0, 1, h);
    g.fillText(a.n, x(t) + 3, 11 * devicePixelRatio);
  }
}

// ---------------------------------------------------------------- diagram

function drawDiagram() {
  const svg = $("#diagram");
  const edge = ([a, b]) => {
    const [x1, y1] = NODES[a], [x2, y2] = NODES[b];
    const sx = x1 + 64, ex = x2 - 64;
    return `<path class="edge" d="M${sx},${y1} C${(sx + ex) / 2},${y1} ${(sx + ex) / 2},${y2} ${ex},${y2}"/>`;
  };
  const node = ([id, [x, y, label, hint]]) => `<g class="node" data-node="${id}">
      <rect x="${x - 64}" y="${y - 27}" width="128" height="54" rx="8"/>
      <text x="${x}" y="${y - 1}" text-anchor="middle">${label}</text>
      <text class="hint" x="${x}" y="${y + 18}" text-anchor="middle">${hint}</text></g>`;
  const row = ([y, a, b]) => `<text class="rowlabel" x="8" y="${b ? y - 4 : y + 5}">${a}</text>` +
    (b ? `<text class="rowlabel" x="8" y="${y + 14}">${b}</text>` : "");
  svg.innerHTML = `
    ${ROWS.map(row).join("")}
    ${EDGES.map(edge).join("")}${Object.entries(NODES).map(node).join("")}
    ${shown().map((k) => `<g class="marker" id="marker-${k}" style="opacity:0">
      <circle r="12" fill="var(${COLORS[k]})" stroke="#0f1115" stroke-width="1.5"/><text text-anchor="middle" y="4.5">${LETTER[k]}</text>
    </g>`).join("")}`;
  for (const k of shown()) placeMarker(k);
}

function placeMarker(k) {
  const el = document.getElementById(`marker-${k}`);
  if (!el) return;
  const L = state.lanes[k];
  if (!L.node) { el.style.opacity = 0; return; }
  const i = shown().indexOf(k);
  const [x, y] = NODES[L.node];
  el.style.opacity = 1;
  el.style.transform = `translate(${x - 39 + i * 26}px, ${y + 36}px)`;
}

function setNode(k, node, phase) {
  const L = state.lanes[k];
  L.node = node;
  if (phase) L.phase = phase;
  placeMarker(k);
}

// ---------------------------------------------------------------- charts

function svgEl(w, h, body, label) {
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${label}">${body}</svg>`;
}

// A round tick step for about four intervals, and the axis top it implies.
function niceScale(v) {
  const raw = Math.max(v, 1e-9) / 4, p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= raw);
  return { step, top: Math.max(step, Math.ceil(v / step) * step) };
}

// An arrow from (x1,y1) to (x2,y2) with a "better" label: points the way the
// reader wants the marks to go.
function betterArrow(x1, y1, x2, y2, label = "better") {
  const a = Math.atan2(y2 - y1, x2 - x1), s = 7;
  const head = [[x2, y2], [x2 - s * Math.cos(a - 0.5), y2 - s * Math.sin(a - 0.5)],
    [x2 - s * Math.cos(a + 0.5), y2 - s * Math.sin(a + 0.5)]];
  const horizontal = y1 === y2;
  const lx = horizontal ? (x1 + x2) / 2 : x1 - 8, ly = horizontal ? y1 - 7 : (y1 + y2) / 2 + 5;
  return `<line class="arrow" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>
    <polygon class="arrowhead" points="${head.map((p) => p.join(",")).join(" ")}"/>
    <text class="tick" x="${lx}" y="${ly}" text-anchor="${horizontal ? "middle" : "end"}">${label}</text>`;
}

// Horizontal bars, one per method, every bar labeled. lower: whether a shorter
// bar is better.
function hbars(values, fmt, lower = true) {
  const lanes = shown().filter((k) => values[k] != null);
  const W = 560, rowH = 40, T = 38, H = T + Math.max(1, lanes.length) * rowH + 10, L0 = 130, R = 90;
  const { step, top } = niceScale(Math.max(...lanes.map((k) => values[k]), lower ? 1 : 0.01));
  const x = (v) => L0 + (v / top) * (W - L0 - R);
  let body = "";
  for (let v = 0; v <= top + step / 2; v += step)
    body += `<line class="grid" x1="${x(v)}" x2="${x(v)}" y1="${T - 4}" y2="${H - 8}"/>`;
  lanes.forEach((k, i) => {
    const y = T + i * rowH, v = values[k], w = Math.max(x(v) - L0, 1.5), r = Math.min(4, w);
    body += `<text class="name" x="${L0 - 10}" y="${y + 23}" text-anchor="end">${title(k)}</text>
      <path class="bar" fill="var(${COLORS[k]})" d="M${L0},${y + 6} H${L0 + w - r} Q${L0 + w},${y + 6} ${L0 + w},${y + 6 + r}
        V${y + 30 - r} Q${L0 + w},${y + 30} ${L0 + w - r},${y + 30} H${L0} Z"><title>${title(k)}: ${fmt(v)}</title></path>
      <text class="value" x="${L0 + w + 8}" y="${y + 24}">${fmt(v)}</text>`;
  });
  body += `<line class="axis" x1="${L0}" x2="${L0}" y1="${T - 4}" y2="${H - 8}"/>`;
  body += lower ? betterArrow(W - R - 10, 24, W - R - 110, 24) : betterArrow(W - R - 110, 24, W - R - 10, 24);
  return svgEl(W, H, body, "bar chart");
}

// Grouped columns: one group per category, one bar per method.
function columns(groups, fmt, highlight = "mitrafsnap") {
  const lanes = shown();
  const W = 560, H = 250, L0 = 52, R = 64, B = 214, T = 26;
  const vals = groups.flatMap((g) => lanes.map((k) => g.values[k]).filter((v) => v != null));
  const { step, top } = niceScale(Math.max(1, ...vals));
  const y = (v) => B - (v / top) * (B - T);
  const gw = (W - L0 - R) / groups.length;
  const bw = Math.min(24, (gw - 22) / lanes.length - 2);
  let body = "";
  for (let v = 0; v <= top + step / 2; v += step) {
    const yy = y(v);
    body += `<line class="grid" x1="${L0}" x2="${W - R}" y1="${yy}" y2="${yy}"/>
      <text class="tick" x="${L0 - 6}" y="${yy + 5}" text-anchor="end">${fmt(v, true)}</text>`;
  }
  groups.forEach((g, gi) => {
    const x0 = L0 + gi * gw + (gw - lanes.length * (bw + 2)) / 2;
    lanes.forEach((k, li) => {
      const v = g.values[k];
      if (v == null) return;
      const x = x0 + li * (bw + 2), yy = Math.min(y(v), B - 1.5), h = B - yy, r = Math.min(4, h);
      body += `<path class="bar" fill="var(${COLORS[k]})" d="M${x},${B} V${yy + r} Q${x},${yy} ${x + r},${yy}
        H${x + bw - r} Q${x + bw},${yy} ${x + bw},${yy + r} V${B} Z"><title>${title(k)}, ${g.label}: ${fmt(v)}</title></path>`;
      if (k === highlight)
        body += `<text class="value" x="${x + bw / 2}" y="${yy - 6}" text-anchor="middle">${fmt(v)}</text>`;
    });
    body += `<text class="cat" x="${L0 + gi * gw + gw / 2}" y="${B + 22}" text-anchor="middle">${g.label}</text>`;
  });
  body += `<line class="axis" x1="${L0}" x2="${W - R}" y1="${B}" y2="${B}"/>` +
    betterArrow(W - 18, T + 10, W - 18, T + 90);
  return svgEl(W, H, body, "column chart");
}

// Lines over the run, with act markers and a crosshair on hover.
function lines(seriesOf, fmt) {
  const lanes = shown();
  const W = 560, H = 250, L0 = 70, R = 96, B = 214, T = 26;
  const all = lanes.flatMap((k) => seriesOf(k));
  const tMax = Math.max(10, now(), ...all.map((p) => p[0]));
  const { step, top } = niceScale(Math.max(1, ...all.map((p) => p[1])));
  const x = (t) => L0 + (t / tMax) * (W - L0 - R), y = (v) => B - (v / top) * (B - T);
  let body = "";
  for (let v = 0; v <= top + step / 2; v += step) {
    const yy = y(v);
    body += `<line class="grid" x1="${L0}" x2="${W - R}" y1="${yy}" y2="${yy}"/>
      <text class="tick" x="${L0 - 6}" y="${yy + 5}" text-anchor="end">${fmt(v, true)}</text>`;
  }
  let lastLabel = -99;
  for (const a of ACTS) {
    const t = state.acts[a.id]?.t;
    if (t == null) continue;
    body += `<line class="actline" x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${B}"/>`;
    if (x(t) - lastLabel < 24) continue;   // acts close together share a label
    body += `<text class="tick" x="${x(t) + 3}" y="${T + 12}">${a.n.replace("Act ", "")}</text>`;
    lastLabel = x(t);
  }
  const ends = [];
  for (const k of lanes) {
    const s = seriesOf(k);
    if (!s.length) continue;
    const d = s.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    body += `<path class="line" stroke="var(${COLORS[k]})" d="${d}"/>`;
    const [lt, lv] = s[s.length - 1];
    body += `<circle class="end" cx="${x(lt)}" cy="${y(lv)}" r="5" fill="var(${COLORS[k]})"/>`;
    ends.push({ y: y(lv), v: lv, x: x(lt) });
  }
  // end labels, skipping ones that would collide
  ends.sort((a, b) => a.y - b.y);
  let lastY = -99;
  for (const e of ends) {
    if (e.y - lastY < 18) continue;
    body += `<text class="value" x="${e.x + 9}" y="${e.y + 5}">${fmt(e.v)}</text>`;
    lastY = e.y;
  }
  body += `<line class="axis" x1="${L0}" x2="${W - R}" y1="${B}" y2="${B}"/>
    <text class="tick" x="${W - R}" y="${B + 22}" text-anchor="end">${Math.round(tMax)} s</text>
    <text class="tick" x="${L0}" y="${B + 22}">0 s</text>
    ${betterArrow(W - 18, T + 10, W - 18, T + 90)}
    <line class="crosshair" x1="0" x2="0" y1="${T}" y2="${B}" style="display:none"/>
    <rect class="hit" x="${L0}" y="${T}" width="${W - L0 - R}" height="${B - T}"
      data-t1="${tMax}" data-x0="${L0}" data-x1="${W - R}"/>`;
  return svgEl(W, H, body, "line chart");
}

function seriesAt(k, t) {
  let v = 0;
  for (const p of state.lanes[k].series) { if (p[0] > t) break; v = p[1]; }
  return v;
}

function chartData() {
  const lanes = shown();
  const ms = Object.fromEntries(lanes.map((k) => [k, measure(state.lanes[k])]));
  const per = (f) => Object.fromEntries(lanes.map((k) => [k, f(ms[k], state.lanes[k])]));
  return {
    downtime: per((m) => m.downtime),
    builds: per((m, L) => totalBuild(L)),
    total: per((m) => m.total),
    toChange: [{ label: "Act 1 · v1", values: per((m) => m.deploy ?? null) },
      ...UPDATES.map((u) => ({ label: `${u.n} · ${u.version}`,
        values: per((m) => m.updates[u.version]?.toChange ?? null) }))],
    downtimeEach: UPDATES.map((u) => ({ label: `${u.n} · ${u.version}`,
      values: per((m) => m.updates[u.version]?.downtime ?? null) })),
    build: ["v1", "v2", "v3", "v4"].map((v) => ({ label: v, values: per((m, L) => buildTime(L, v)) })),
  };
}

const sec = (v, tick) => (tick ? `${Math.round(v)}` : secs(v));
const meg = (v, tick) => (tick && v === 0 ? "0" : mb(v));
const figure = (name, sub, better, svg) => `<figure class="chart"><figcaption><strong>${name}</strong>
  <span>${sub}</span><span class="better">${better}</span></figcaption>${svg}</figure>`;

function renderCharts() {
  if (!state.info) return;
  const box = $("#charts");
  const data = chartData();
  $("#chart-legend").innerHTML = shown().map((k) =>
    `<span class="key"><span class="dot" style="background:var(${COLORS[k]})"></span>${title(k)}</span>`).join("");
  if (state.tables) { box.innerHTML = tables(data); return; }
  const empty = (vals) => Object.values(vals).every((v) => v == null);
  const wait = (when) => `<p class="tick">shown ${when}</p>`;
  box.innerHTML = [
    figure("Total downtime", "seconds the camera showed nothing, summed over Acts 2 to 4",
      "↓ less is better", empty(data.downtime) ? wait("from Act 2") : hbars(data.downtime, secs)),
    figure("Time until a change is live", "from the start of each act to the camera's first frame of the new version",
      "↓ lower is better", columns(data.toChange, sec)),
    figure("Total time", "Act 1 to Act 4, from each act's start to the change being live",
      "↓ less is better", empty(data.total) ? wait("after Act 1") : hbars(data.total, secs)),
    figure("Total build and push time", "every version built so far; Docker and eStargz rebuild every layer after a change",
      "↓ less is better", empty(data.builds) ? wait("after Act 1") : hbars(data.builds, secs)),
    figure("Downloaded over the link", "cumulative; lazy methods fetch only what the app reads",
      "↓ less is better", lines((k) => state.lanes[k].series, meg)),
    figure("Downtime per update", "seconds without output while each update rolled out",
      "↓ less is better", columns(data.downtimeEach, sec)),
  ].join("");
  for (const hit of box.querySelectorAll("rect.hit")) wireCrosshair(hit);
}

function tables(data) {
  const lanes = shown();
  const table = (name, rows) => `<figure class="chart"><figcaption><strong>${name}</strong></figcaption>
    <table><thead><tr><th></th>${lanes.map((k) => `<th>${title(k)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(([label, values, fmt]) => `<tr><th>${label}</th>${lanes.map((k) =>
      `<td>${values[k] != null ? fmt(values[k]) : "–"}</td>`).join("")}</tr>`).join("")}</tbody></table></figure>`;
  const bytes = Object.fromEntries(lanes.map((k) => [k, state.lanes[k].bytes]));
  return table("Totals", [["Total downtime", data.downtime, secs], ["Total time", data.total, secs],
      ["Total build and push", data.builds, secs], ["Downloaded", bytes, mb]]) +
    table("Time until a change is live", data.toChange.map((g) => [g.label, g.values, secs])) +
    table("Downtime per update", data.downtimeEach.map((g) => [g.label, g.values, secs])) +
    table("Build and push per version", data.build.map((g) => [g.label, g.values, secs]));
}

function wireCrosshair(hit) {
  const svg = hit.ownerSVGElement, line = svg.querySelector(".crosshair"), tip = $("#tip");
  const { t1, x0, x1 } = Object.fromEntries(Object.entries(hit.dataset).map(([k, v]) => [k, +v]));
  hit.onmousemove = (e) => {
    const p = svg.createSVGPoint();
    p.x = e.clientX; p.y = e.clientY;
    const sx = p.matrixTransform(svg.getScreenCTM().inverse()).x;
    const t = ((sx - x0) / (x1 - x0)) * t1;
    line.setAttribute("x1", sx); line.setAttribute("x2", sx); line.style.display = "";
    tip.innerHTML = `<strong>${Math.round(t)} s</strong>` + shown().map((k) =>
      `<div><span class="dot" style="background:var(${COLORS[k]})"></span>${title(k)} ${mb(seriesAt(k, t))}</div>`).join("");
    tip.style.left = `${e.pageX + 14}px`; tip.style.top = `${e.pageY + 10}px`; tip.hidden = false;
  };
  hit.onmouseleave = () => { line.style.display = "none"; tip.hidden = true; };
}

// ---------------------------------------------------------------- intro cards

// Title cards for recorded videos: ?intro=1 (setup) or ?intro=2 (the acts),
// with &speed=N for the playback speed.
const ABOUT = {
  docker: "pulls the whole image, then starts; updates by replacing the container",
  estargz: "starts lazily and fetches files on first read; updates by replacing the container",
  twodfs: "pulls only the selected parts of the image; updates by replacing the container",
  mitrafsnap: "fetches the selected parts lazily; updates the running container's files in place",
};

function renderIntro(slide, speed) {
  const { info } = state;
  const box = document.createElement("div");
  box.className = "intro";
  const link = !info.link.emulated ? "the real network from a registry on another"
    : info.settings.link_mbit ? `a ${info.settings.link_mbit} Mbit/s emulated edge` : "an unlimited emulated edge";
  if (slide === 1) {
    box.innerHTML = `<p class="kicker">MitraFSnap live demo</p>
      <h1>${info.scenario.title}</h1>
      <p class="lead">${info.scenario.description}</p>
      <ul class="methods">${shown().map((k) => `<li><span class="dot" style="background:var(${COLORS[k]})"></span>
        <strong>${title(k)}</strong> ${ABOUT[k]}</li>`).join("")}</ul>
      <p class="facts">All ${shown().length} methods build the same image and run at the same time on one
        machine (${info.backend.arch}, ${info.backend.cpus} CPUs, ${DEVICE_NAME[effectiveDevice()]} inference),
        pulling over ${link} link.</p>
      <p class="speed">This video plays at <strong>${speed}× speed</strong>. Every time shown in the
        dashboard is measured in real time.</p>`;
  } else {
    box.innerHTML = `<p class="kicker">MitraFSnap live demo · ${info.scenario.title}</p>
      <h1>Four acts</h1>
      <ol class="actlist">${ACTS.map((a) => actInfo(a.id)).map((a) =>
        `<li><span class="n">${a.n}</span><div><strong>${a.title}</strong><p>${narration(a.id)}</p></div></li>`).join("")}</ol>`;
  }
  document.body.append(box);
}

// ---------------------------------------------------------------- events

function onEvent(ev) {
  if (ev.t != null) { state.clock = ev.t; state.clockAt = performance.now(); }
  const L = ev.lane ? state.lanes[ev.lane] : null;
  switch (ev.kind) {
    case "stats":
      for (const [k, s] of Object.entries(ev.lanes)) {
        const lane = state.lanes[k];
        if (!lane) continue;
        lane.bytes = s.bytes;
        const last = lane.series[lane.series.length - 1];
        if (last && ev.t < last[0]) continue;   // never draw backwards
        if (!last || ev.t - last[0] >= 1 || s.bytes !== last[1]) lane.series.push([ev.t, s.bytes]);
      }
      return;
    case "frame":
      onFrame(L, ev);
      return;
    case "act":
      state.acts[ev.act] = { state: ev.state, t: ev.state === "start" ? ev.t : state.acts[ev.act]?.t };
      state.running = ev.state === "start" ? ev.act : null;
      if (ev.state === "start") state.lastAct = ev.act;
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
      state.acts = {};
      renderActs();
      drawDiagram();
      log("reset");
      break;
    case "settings":
      if (ev.settings.scenario !== state.info.scenario.name) { connect(); return; }
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
      L.rolling = true;
      break;
    case "ready": {
      const v = (ev.what || "").split(" ")[0];
      if (/^v\d$/.test(v)) { L.live[v] = ev.t; L.rolling = false; }
      break;
    }
    case "lane_error":
      L.phase = "error";
      log(`${ev.lane}: ${ev.error}`);
      break;
  }
  if (ev.lane) updateLane(ev.lane);
}

function onPhase(L, ev) {
  const k = L.key;
  const label = { build: "building", push: "pushing", pull: "pulling", start: "starting",
    stop: "stopping", refresh: "refreshing" }[ev.phase] || ev.phase;
  if (ev.state === "start") {
    const node = {
      build: "build", push: "push", pull: L.rolling ? "r_pull" : "pull",
      start: L.rolling ? "r_start" : "start", stop: "r_stop", refresh: "f_refresh",
    }[ev.phase];
    setNode(k, node, label);
  } else if (ev.state === "end") {
    if (ev.phase === "build" || ev.phase === "push") {
      L[ev.phase][ev.version] = ev.seconds;
      if (ev.phase === "build" && ev.of) L.rebuilt[ev.version] = { rebuilt: ev.rebuilt, of: ev.of };
      if (ev.phase === "push" && !L.rolling) setNode(k, recentlyServing(L) ? "serve" : "registry",
        recentlyServing(L) ? "serving" : "published");
    }
    const r = ev.of ? `, rebuilt ${ev.rebuilt} of ${ev.of}` : "";
    log(`${k}: ${ev.phase} ${secs(ev.seconds)}${r}${ev.bytes ? `, ${mb(ev.bytes)} over the link` : ""}`);
  } else if (ev.state === "error") {
    L.phase = `${ev.phase} failed`;
    log(`${k}: ${ev.phase} failed: ${ev.error}`);
  }
}

function onApp(L, ev) {
  const k = L.key;
  if (ev.phase === "load_start") setNode(k, L.rolling ? (k === "mitrafsnap" ? "f_reopen" : "r_load") : "load", `loading ${ev.model}`);
  if (ev.phase === "reload_start") setNode(k, "f_reopen", "reopening files");
  if (ev.phase === "load_done") log(`${k}: loaded ${ev.model} in ${secs(ev.seconds)}`);
  if (ev.phase === "reload_done") log(`${k}: reloaded ${ev.models.join(", ") || "nothing"} in ${secs(ev.seconds)}`);
}

function recentlyServing(L) { const t = L.frames[L.frames.length - 1]; return t && now() - t < 1.5; }

function onFrame(L, ev) {
  L.frames.push(ev.t);
  if (L.frames.length > 20000) L.frames.splice(0, 5000);
  L.version = ev.version;
  L.models = ev.models || [];
  if (L.firstFrameAt == null) L.firstFrameAt = ev.t;
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
  if (state.info) for (const k of shown()) { updateLane(k); drawTimeline(k); }
  requestAnimationFrame(tick);
}

$("#connect").onclick = connect;
$("#webcam").onclick = toggleWebcam;
$("#all-lanes").onclick = () => setLanes(state.info.methods.map((m) => m.key));
$("#chart-mode").onclick = (e) => {
  state.tables = !state.tables;
  e.target.textContent = state.tables ? "Show charts" : "Show as tables";
  renderCharts();
};
$("#apply-machines").onclick = async () => {
  try {
    await api("/api/settings", { registry: $("#registry").value.trim(), builder: $("#builder").value.trim(),
      builder_token: $("#builder-token").value });
    log("machines updated; Act 1 prepares the builder and registry");
    await connect();
  } catch (err) { log(`machines: ${err.message}`); $("#narration").textContent = err.message; }
};
$("#link").onchange = async (e) => {
  try { await api("/api/settings", { link_mbit: Number(e.target.value) }); } catch (err) { log(err.message); }
};
$("#device").onchange = async (e) => {
  try {
    await api("/api/settings", { device: e.target.value });
    log(`device: ${e.target.value}; it applies from the next Act 1`);
  } catch (err) { log(err.message); e.target.value = state.info.settings.device; }
};
$("#scenario").onchange = async (e) => {
  try { await api("/api/settings", { scenario: e.target.value }); await connect(); } catch (err) { log(err.message); }
};
loadPrefs();
const introSlide = Number(new URLSearchParams(location.search).get("intro"));
connect().then(() => {
  if (introSlide) renderIntro(introSlide, new URLSearchParams(location.search).get("speed") || "1");
});
requestAnimationFrame(tick);
setInterval(renderCharts, 1000);
