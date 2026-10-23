// MitraFSnap demo dashboard. Talks to the backend only through its HTTP API,
// so the backend can run on this machine or anywhere else.
"use strict";

const ACTS = [
  { id: "prepare", n: "Setup", title: "Prepare", what: "models, clip, base images" },
  { id: "build-v1", n: "Act 1", title: "Build v1", what: "build and push the image" },
  { id: "deploy", n: "Act 2", title: "Deploy", what: "camera needs the detector only" },
  { id: "add-model", n: "Act 3", title: "Add segmenter", what: "v2: build + roll out" },
  { id: "build-v3", n: "Act 4", title: "Build v3", what: "new detector + classes" },
  { id: "rollout-v3", n: "Act 5", title: "Roll out v3", what: "replace vs refresh in place" },
  { id: "patch", n: "Act 6", title: "Patch unused model", what: "v4: build + roll out" },
];
// Which act rolls out which version, for metrics and charts.
const ROLLOUTS = [
  { act: "add-model", version: "v2", short: "Act 3" },
  { act: "rollout-v3", version: "v3", short: "Act 5" },
  { act: "patch", version: "v4", short: "Act 6" },
];
// One line per act for the caption under the act bar.
const NARRATION = {
  prepare: "Downloading the models and the clip, and publishing the lane runtime in three base formats.",
  reset: "Clearing every lane, image and cache, so the next run starts cold.",
  "build-v1": "Every method builds and pushes the same image. 2DFS and MitraFSnap build one allotment per model in parallel; BuildKit builds the layers one after another.",
  deploy: "The camera needs only the detector. Docker and 2DFS download before they start; eStargz and MitraFSnap start lazily and fetch only what the app reads.",
  "add-model": "v2 ships an instance segmenter the first image did not contain. Every method rebuilds and rolls it out; Docker, eStargz and 2DFS restart their containers, MitraFSnap refreshes the running one and fetches the model when the app opens it.",
  "build-v3": "v3 ships a stronger detector that also recognizes bicycles, buses, trucks and streetcars. Only the changed parts are rebuilt.",
  "rollout-v3": "Rolling out v3. Docker, eStargz and 2DFS stop, pull and restart. MitraFSnap refreshes the running container's files in place and the app swaps models while it keeps serving.",
  patch: "v4 patches a 528 MB model this camera never loads. Replacement restarts the camera anyway; MitraFSnap refreshes in milliseconds and fetches nothing.",
};
const COLORS = { docker: "--docker", estargz: "--estargz", twodfs: "--twodfs", mitrafsnap: "--mitrafsnap" };
const LETTER = { docker: "D", estargz: "E", twodfs: "2", mitrafsnap: "M" };
const TAGS = {
  docker: ["eager pull", "replace"],
  estargz: ["lazy pull", "replace"],
  twodfs: ["partial eager", "replace"],
  mitrafsnap: ["partial lazy", "refresh in place"],
};
const DEVICE_NAME = { cuda: "NVIDIA GPU", apple: "Apple GPU", cpu: "CPU" };

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
    build: {}, push: {}, deployAt: null, firstFrame: null,
    rollouts: {},        // version -> {at, live, bytes0, bytes1}
    rolling: false,      // a rollout is in progress on this lane
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
  for (const m of state.info.methods) state.lanes[m.key] = newLane(m.key);
  state.acts = {};
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
  state.es = new EventSource(url("/api/events"));
  state.es.onopen = () => setStatus(statusText(), "ok");
  state.es.onerror = () => setStatus("reconnecting", "bad");
  state.es.onmessage = (m) => onEvent(JSON.parse(m.data));
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
  if (settings.device === "cpu") return "cpu";
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
  $("#link").value = String(info.settings.link_mbit || 0);
  $("#device").value = info.settings.device || "auto";
  const acc = info.backend.accelerators;
  $("#device option[value=gpu]").disabled = !acc.cuda && !acc.apple;
  $("#device option[value=gpu]").textContent = acc.cuda ? "NVIDIA GPU" : acc.apple ? "Apple GPU" : "GPU (none)";
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

// Act labels and narration, with the scenario's overrides.
function actInfo(id) {
  const base = ACTS.find((a) => a.id === id);
  const o = state.info?.scenario.acts?.[id] || {};
  const add = state.info?.scenario.add;
  const t = base && id === "add-model" && add ? `Add ${add}` : base?.title;
  return base && { ...base, title: o.title || t, what: o.what || base.what,
    narration: o.narration || NARRATION[id] };
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
  const text = act ? act.narration : NARRATION[state.lastAct];
  $("#narration").innerHTML = text
    ? `<strong>${act ? `${act.n} · ${act.title}` : "Reset"}</strong> ${text}` : "";
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
  $("#lanes").classList.toggle("few", lanes.length <= 2);
  const add = cap(state.info.scenario.add);
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
        ${metric("build", "Build + push")}${metric("down", "Downloaded")}${metric("first", "First frame after deploy")}
        ${metric("live-v2", `${add} live after`)}${metric("live-v3", "v3 live after")}${metric("live-v4", "v4 live after")}
        ${metric("gap-v2", `Blind · Act 3`)}${metric("gap-v3", "Blind · Act 5")}${metric("gap-v4", "Blind · Act 6")}
      </div>
      </div>
    </article>`).join("");
  drawDiagram();
}

function metric(id, label) {
  return `<div class="metric"><div class="k">${label}</div><div class="v" data-m="${id}">–</div></div>`;
}

function laneEl(k, sel) { return document.querySelector(`#lane-${k} ${sel}`); }

function buildTime(L, v) { return L.build[v] != null ? L.build[v] + (L.push[v] || 0) : null; }

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
  const latest = ["v4", "v3", "v2"].find((v) => buildTime(L, v) != null);
  set("build", latest ? `${secs(buildTime(L, latest))} <small>${latest} · v1 ${secs(buildTime(L, "v1"))}</small>`
    : secs(buildTime(L, "v1")));
  set("down", mb(L.bytes));
  set("first", L.firstFrame != null ? secs(L.firstFrame) : L.deployAt != null ? "…" : "–");
  for (const { version: v } of ROLLOUTS) {
    const r = L.rollouts[v];
    if (!r) { set(`live-${v}`, "–"); set(`gap-${v}`, "–"); continue; }
    const gap = blindTime(L, r);
    set(`live-${v}`, r.live != null ? secs(r.live) : "…");
    set(`gap-${v}`, r.live != null && gap === 0 ? "0 s" : secs(gap),
      r.live != null && gap === 0 ? "good" : gap ? "bad" : "");
  }
}

// Seconds without output between the start of a rollout and the new version
// going live (or now, while it hasn't). Gaps under BLIND are slow frames on a
// loaded CPU, not an outage.
const BLIND = 1.0;
function blindTime(L, r) {
  const start = r.at, end = r.live != null ? r.at + r.live : null;
  const until = end ?? now();
  let blind = 0, last = start;
  for (const t of L.frames) {
    if (t <= start) continue;
    if (t > until + 0.05) break;   // the first new-version frame closes the window
    if (t - last > BLIND) blind += t - last;
    last = t;
  }
  if (end == null && until - last > BLIND) blind += until - last;
  return blind;
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
  const deployed = L.deployAt != null ? L.deployAt : Infinity;
  // red where the lane should have been serving but produced nothing
  g.fillStyle = "rgba(239,107,107,.35)";
  let last = Math.max(t0, deployed);
  for (const t of L.frames.concat([t1])) {
    if (t < t0) { last = Math.max(last, t); continue; }
    if (t - last > BLIND && last >= deployed) g.fillRect(x(last), 0, x(t) - x(last), h);
    last = t;
  }
  g.fillStyle = cssVar(COLORS[k]);
  for (const t of L.frames) if (t >= t0) g.fillRect(x(t), 8, 1.5, h - 16);
  g.fillStyle = "rgba(230,232,238,.55)";
  g.font = `${11 * devicePixelRatio}px ${cssVar("--font")}`;
  for (const [act, a] of Object.entries(state.acts)) {
    if (a.t == null || a.t < t0) continue;
    g.fillRect(x(a.t), 0, 1, h);
    g.fillText(ACTS.find((z) => z.id === act)?.n ?? act, x(a.t) + 3, 10 * devicePixelRatio);
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
      <text class="hint" x="${x}" y="${y + 17}" text-anchor="middle">${hint}</text></g>`;
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

// Every chart reads the same lane state the metrics use, so live runs and
// trace replay draw identical charts.
function svgEl(w, h, body, label) {
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${label}">${body}</svg>`;
}

// A round tick step for about four intervals, and the axis top it implies.
function niceScale(v) {
  const raw = Math.max(v, 1e-9) / 4, p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= raw);
  return { step, top: Math.max(step, Math.ceil(v / step) * step) };
}

// Grouped columns: one group per category, one bar per method.
function columns(groups, unit, fmt, highlight = "mitrafsnap") {
  const lanes = shown();
  const W = 520, H = 230, L0 = 44, B = 196, T = 22;
  const vals = groups.flatMap((g) => lanes.map((k) => g.values[k]).filter((v) => v != null));
  const { step, top } = niceScale(Math.max(1, ...vals));
  const y = (v) => B - (v / top) * (B - T);
  const gw = (W - L0 - 8) / groups.length;
  const bw = Math.min(24, (gw - 22) / lanes.length - 2);
  let body = "";
  for (let v = 0; v <= top + step / 2; v += step) {
    const yy = y(v);
    body += `<line class="grid" x1="${L0}" x2="${W - 4}" y1="${yy}" y2="${yy}"/>
      <text class="tick" x="${L0 - 6}" y="${yy + 4}" text-anchor="end">${fmt(v, true)}</text>`;
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
        body += `<text class="value" x="${x + bw / 2}" y="${yy - 5}" text-anchor="middle">${fmt(v)}</text>`;
    });
    body += `<text class="cat" x="${L0 + gi * gw + gw / 2}" y="${B + 18}" text-anchor="middle">${g.label}</text>`;
  });
  body += `<line class="axis" x1="${L0}" x2="${W - 4}" y1="${B}" y2="${B}"/>
    <text class="tick" x="${L0}" y="12">${unit}</text>`;
  return svgEl(W, H, body, unit);
}

// Lines over the run, with act markers and a crosshair on hover.
function lines(seriesOf, unit, fmt) {
  const lanes = shown();
  const W = 520, H = 230, L0 = 50, R = 70, B = 196, T = 22;
  const all = lanes.flatMap((k) => seriesOf(k));
  const tMax = Math.max(10, now(), ...all.map((p) => p[0]));
  const { step, top } = niceScale(Math.max(1, ...all.map((p) => p[1])));
  const x = (t) => L0 + (t / tMax) * (W - L0 - R), y = (v) => B - (v / top) * (B - T);
  let body = "";
  for (let v = 0; v <= top + step / 2; v += step) {
    const yy = y(v);
    body += `<line class="grid" x1="${L0}" x2="${W - R}" y1="${yy}" y2="${yy}"/>
      <text class="tick" x="${L0 - 6}" y="${yy + 4}" text-anchor="end">${fmt(v, true)}</text>`;
  }
  let lastLabel = -99;
  for (const a of ACTS) {
    const t = state.acts[a.id]?.t;
    if (t == null || a.id === "prepare") continue;
    body += `<line class="actline" x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${B}"/>`;
    if (x(t) - lastLabel < 20) continue;   // acts close together share a label
    body += `<text class="tick" x="${x(t) + 3}" y="${T + 10}">${a.n.replace("Act ", "")}</text>`;
    lastLabel = x(t);
  }
  const ends = [];
  for (const k of lanes) {
    const s = seriesOf(k);
    if (!s.length) continue;
    const d = s.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    body += `<path class="line" stroke="var(${COLORS[k]})" d="${d}"/>`;
    const [lt, lv] = s[s.length - 1];
    body += `<circle class="end" cx="${x(lt)}" cy="${y(lv)}" r="4" fill="var(${COLORS[k]})"/>`;
    ends.push({ k, y: y(lv), v: lv, x: x(lt) });
  }
  // end labels, skipping ones that would collide
  ends.sort((a, b) => a.y - b.y);
  let lastY = -99;
  for (const e of ends) {
    if (e.y - lastY < 14) continue;
    body += `<text class="value" x="${e.x + 8}" y="${e.y + 4}">${fmt(e.v)}</text>`;
    lastY = e.y;
  }
  body += `<line class="axis" x1="${L0}" x2="${W - R}" y1="${B}" y2="${B}"/>
    <text class="tick" x="${L0}" y="12">${unit}</text>
    <text class="tick" x="${W - R}" y="${B + 18}" text-anchor="end">${Math.round(tMax)} s</text>
    <text class="tick" x="${L0}" y="${B + 18}">0 s</text>
    <line class="crosshair" x1="0" x2="0" y1="${T}" y2="${B}" style="display:none"/>
    <rect class="hit" x="${L0}" y="${T}" width="${W - L0 - R}" height="${B - T}"
      data-t0="0" data-t1="${tMax}" data-x0="${L0}" data-x1="${W - R}"/>`;
  return svgEl(W, H, body, unit);
}

function seriesAt(k, t) {
  const s = state.lanes[k].series;
  let v = 0;
  for (const p of s) { if (p[0] > t) break; v = p[1]; }
  return v;
}

function chartData() {
  const lanes = shown(), L = (k) => state.lanes[k];
  const per = (f) => Object.fromEntries(lanes.map((k) => [k, f(L(k))]));
  const add = cap(state.info.scenario.add);
  return {
    blind: ROLLOUTS.map((r) => ({ label: `${r.short} · ${r.version}`, values: per((x) =>
      x.rollouts[r.version] ? blindTime(x, x.rollouts[r.version]) : null) })),
    live: [{ label: "Act 2 · deploy", values: per((x) => x.firstFrame) },
      ...ROLLOUTS.map((r) => ({ label: `${r.short} · ${r.version}`, values: per((x) => x.rollouts[r.version]?.live ?? null) }))],
    build: ["v1", "v2", "v3", "v4"].map((v) => ({ label: v === "v2" ? `v2 · +${add.toLowerCase()}` : v,
      values: per((x) => buildTime(x, v)) })),
  };
}

const sec = (v, tick) => (tick ? `${Math.round(v)}` : secs(v));
const meg = (v, tick) => (tick && v === 0 ? "0" : mb(v));

function renderCharts() {
  if (!state.info) return;
  const box = $("#charts");
  const data = chartData();
  const legend = shown().map((k) =>
    `<span class="key"><span class="dot" style="background:var(${COLORS[k]})"></span>${title(k)}</span>`).join("");
  $("#chart-legend").innerHTML = legend;
  if (state.tables) { box.innerHTML = tables(data); return; }
  box.innerHTML = `
    <figure class="chart"><figcaption><strong>Downloaded over the link</strong>
      <span>cumulative, all acts; lazy methods fetch only what the app reads</span></figcaption>
      ${lines((k) => state.lanes[k].series, "downloaded", meg)}</figure>
    <figure class="chart"><figcaption><strong>Time without output per rollout</strong>
      <span>lower is better; MitraFSnap keeps serving through every update</span></figcaption>
      ${columns(data.blind, "seconds", sec)}</figure>
    <figure class="chart"><figcaption><strong>Time until the new version serves</strong>
      <span>first frame after deploy, then each rollout; lower is better</span></figcaption>
      ${columns(data.live, "seconds", sec)}</figure>
    <figure class="chart"><figcaption><strong>Build and push per version</strong>
      <span>only changed parts are rebuilt with independent allotments</span></figcaption>
      ${columns(data.build, "seconds", sec)}</figure>`;
  for (const hit of box.querySelectorAll("rect.hit")) wireCrosshair(hit);
}

function tables(data) {
  const lanes = shown();
  const table = (name, groups, fmt) => `<figure class="chart"><figcaption><strong>${name}</strong></figcaption>
    <table><thead><tr><th></th>${lanes.map((k) => `<th>${title(k)}</th>`).join("")}</tr></thead>
    <tbody>${groups.map((g) => `<tr><th>${g.label}</th>${lanes.map((k) =>
      `<td>${g.values[k] != null ? fmt(g.values[k]) : "–"}</td>`).join("")}</tr>`).join("")}</tbody></table></figure>`;
  return table("Downloaded over the link", [{ label: "so far", values: Object.fromEntries(
      lanes.map((k) => [k, state.lanes[k].bytes])) }], mb) +
    table("Time without output per rollout", data.blind, secs) +
    table("Time until the new version serves", data.live, secs) +
    table("Build and push per version", data.build, secs);
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
  const link = info.settings.link_mbit ? `${info.settings.link_mbit} Mbit/s` : "an unlimited";
  if (slide === 1) {
    box.innerHTML = `<p class="kicker">MitraFSnap live demo</p>
      <h1>${info.scenario.title}</h1>
      <p class="lead">${info.scenario.description}</p>
      <ul class="methods">${shown().map((k) => `<li><span class="dot" style="background:var(${COLORS[k]})"></span>
        <strong>${title(k)}</strong> ${ABOUT[k]}</li>`).join("")}</ul>
      <p class="facts">All ${shown().length} methods build the same image and run at the same time on one
        machine (${info.backend.arch}, ${info.backend.cpus} CPUs, ${DEVICE_NAME[effectiveDevice()]} inference),
        pulling over ${link} emulated edge link.</p>
      <p class="speed">This video plays at <strong>${speed}× speed</strong>. Every time shown in the
        dashboard is measured in real time.</p>`;
  } else {
    box.innerHTML = `<p class="kicker">MitraFSnap live demo · ${info.scenario.title}</p>
      <h1>Six acts</h1>
      <ol class="actlist">${ACTS.filter((a) => a.id !== "prepare").map((a) => actInfo(a.id)).map((a) =>
        `<li><span class="n">${a.n}</span><div><strong>${a.title}</strong><p>${a.narration}</p></div></li>`).join("")}</ol>`;
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
        if (!last || ev.t - last[0] >= 1 || s.bytes !== last[1]) lane.series.push([ev.t, s.bytes]);
      }
      return;
    case "frame":
      onFrame(L, ev);
      return;
    case "act":
      state.acts[ev.act] = { state: ev.state, t: ev.state === "start" ? ev.t : state.acts[ev.act]?.t };
      state.running = ev.state === "start" ? ev.act : null;
      if (ev.state === "start") { state.lastAct = ev.act; onActStart(ev); }
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
      L.rollouts[ev.version] = { at: ev.t, live: null, bytes0: L.bytes, bytes1: null };
      L.rolling = true;
      break;
    case "ready": {
      const v = (ev.what || "").split(" ")[0];
      if (L.rollouts[v]) { L.rollouts[v].live = ev.seconds; L.rollouts[v].bytes1 = L.bytes; L.rolling = false; }
      break;
    }
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
  }
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
      if (ev.phase === "push" && !L.rolling) setNode(k, recentlyServing(L) ? "serve" : "registry",
        recentlyServing(L) ? "serving" : "published");
    }
    log(`${k}: ${ev.phase} ${secs(ev.seconds)}${ev.bytes ? `, ${mb(ev.bytes)} over the link` : ""}`);
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
  if (L.frames.length > 4000) L.frames.splice(0, 1000);
  L.version = ev.version;
  L.models = ev.models || [];
  if (L.deployAt != null && L.firstFrame == null) L.firstFrame = ev.t - L.deployAt;
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
$("#link").onchange = async (e) => {
  try { await api("/api/settings", { link_mbit: Number(e.target.value) }); } catch (err) { log(err.message); }
};
$("#device").onchange = async (e) => {
  try {
    await api("/api/settings", { device: e.target.value });
    log(`device: ${e.target.value}; run Prepare again to apply it`);
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
