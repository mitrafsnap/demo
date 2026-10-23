"""Demo controller: HTTP API for the dashboard, act orchestration, and the
relay between the frame source, the lane apps and the browser.

Public API (dashboard):
    GET  /api/info                       backend, scenarios, methods, settings
    POST /api/settings                   scenario, lanes, link_mbit, cpus, ...
    POST /api/acts/{act}                 prepare | build-v1 | deploy | enable |
                                         build-v2 | update | patch | reset
    GET  /api/events                     server-sent events
    GET  /api/trace                      recorded events of the current run
    GET  /api/video                      WebSocket: every lane's annotated frames,
                                         each message = lane index byte + JPEG
    GET  /api/lanes/{lane}/video         MJPEG of one lane's annotated output
    GET  /api/source/video               MJPEG of the raw frames
    GET  /api/camera                     WebSocket: browser webcam frames in

Internal API (lane apps, localhost only):
    GET  /internal/frames/next           next frame after ?after=seq
    POST /internal/lanes/{lane}/frame    annotated JPEG + X-Meta header
    POST /internal/lanes/{lane}/event    app event (load, reload, serving, ...)
"""

import asyncio
import collections
import json
import os
import platform
import time
import traceback

import cv2
import numpy as np
from aiohttp import web, WSMsgType

import frames
import link
import methods
import scenario
from bus import Bus

DASHBOARD = os.environ.get("DEMO_DASHBOARD", "/opt/demo/dashboard")
LANE_DIR = os.environ.get("DEMO_LANE", "/opt/demo/lane")
TOKEN = os.environ.get("DEMO_TOKEN", "")
ACTS = ["prepare", "build-v1", "deploy", "enable", "build-v2", "update", "patch", "reset"]


class Session:
    def __init__(self):
        self.bus = Bus()
        self.source = frames.FrameSource()
        self.link = link.Link(("registry", 5000), 200)
        self.settings = {"scenario": "traffic", "lanes": ["docker", "mitrafsnap"],
                         "mode": "live", "link_mbit": 200, "cpus": 2, "threads": 2,
                         "watch_interval": "1s", "auto_watch": False, "cold_build": True,
                         "device": "auto"}
        self.settings["scn"] = scenario.load(self.settings["scenario"])
        self.methods = {M.key: M(i, self.bus, self.link, self.settings)
                        for i, M in enumerate(methods.METHODS)}
        self.frames = {k: collections.deque(maxlen=200) for k in self.methods}
        self.latest = {}
        self.new_frame = {k: asyncio.Event() for k in self.methods}
        self.status = {k: "idle" for k in self.methods}
        self.act = None
        self.done = set()
        self.prepared = False

    async def start(self):
        for i, key in enumerate(self.methods):
            await self.link.add(key, 5101 + i)
        self.source.start()
        asyncio.create_task(self.stats_loop())
        # Containers from a previous controller survive in containerd; start clean.
        asyncio.create_task(self.run_act("reset"))

    def public_settings(self):
        return {k: v for k, v in self.settings.items() if k != "scn"}

    def lanes(self):
        return [self.methods[k] for k in self.settings["lanes"]]

    # -- acts ----------------------------------------------------------------

    async def run_act(self, name):
        self.act = name
        self.bus.emit("act", act=name, state="start", lanes=self.settings["lanes"])
        t0 = time.monotonic()
        try:
            await getattr(self, "act_" + name.replace("-", "_"))()
            self.done.add(name)
            self.bus.emit("act", act=name, state="end", seconds=round(time.monotonic() - t0, 3))
        except Exception as e:
            traceback.print_exc()
            self.bus.emit("act", act=name, state="error", error=str(e)[-800:])
        finally:
            self.act = None

    async def each(self, fn):
        failed = []

        async def one(m):
            try:
                await fn(m)
            except Exception as e:
                traceback.print_exc()
                failed.append(m.title)
                m.emit("lane_error", error=str(e)[-800:])
        await asyncio.gather(*(one(m) for m in self.lanes()))
        if failed:
            raise RuntimeError(f"failed on {', '.join(failed)}")

    async def wait_for(self, m, what, pred, since, timeout=900):
        """Wait until the lane's app produces a frame matching pred, so an act
        ends when the audience can see its effect, not when commands return."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            latest = self.latest.get(m.key)
            if latest and latest[3] >= since and pred(latest[2]):
                m.emit("ready", what=what, seconds=round(latest[3] - since, 3))
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"{m.title}: no {what} after {timeout}s")

    async def act_prepare(self):
        scn = self.settings["scn"]
        await scenario.fetch_assets(scn, self.bus.emit)
        dev = methods.device(self.settings["device"])
        self.settings["effective_device"] = dev
        methods.BASE_TAG = methods.base_tag(dev)
        if not await asyncio.to_thread(methods.base_ready):
            await methods.prepare_base(LANE_DIR, self.bus.emit, dev)
        for v in scn["versions"]:
            scenario.materialize(scn, v)
        src = scn["frames"]
        # A camera scenario plays its fallback clip until a browser sends webcam frames.
        clip = src if src["kind"] == "file" else src.get("fallback")
        if clip:
            path = scenario.asset_path(clip["asset"], scn["assets"][clip["asset"]])
            self.bus.emit("prepare", step="decode clip")
            decoded = await asyncio.to_thread(frames.decode_clip, path, src["fps"], src["width"],
                                              clip.get("start", 0), clip.get("end"))
            self.source.load_clip(decoded, src["fps"])
        self.prepared = True
        self.bus.emit("prepare", step="ready", sizes={v: scenario.slot_sizes(scn, v)
                                                     for v in scn["versions"]})

    async def act_build_v1(self):
        if self.settings["cold_build"]:
            await self.each(lambda m: m.clean_build_cache())
        await self.each(lambda m: m.build("v1"))

    async def act_deploy(self):
        scn = self.settings["scn"]
        enabled = [s for s in scn["select"] if s != "config"]
        since = time.monotonic()

        async def deploy(m):
            await m.deploy(scn["select"], enabled)
            await self.wait_for(m, "first frame", lambda meta: True, since)
        await self.each(deploy)

    async def act_enable(self):
        model = self.settings["scn"].get("enable", "segmenter")
        since = time.monotonic()

        async def enable(m):
            status, msg = await m.enable(model)
            if status == 409 and m.partial and not m.lazy:
                # 2DFS pulled only the selected allotments: redeploy with a wider selection.
                m.emit("redeploy", reason=msg)
                await m.stop()
                await m.deploy(sorted(set(m.selected) | {model}), m.enabled + [model])
            await self.wait_for(m, f"{model} output",
                                lambda meta: model in meta.get("models", []), since)
        await self.each(enable)

    async def act_build_v2(self):
        await self.each(lambda m: m.build("v2"))

    async def act_update(self):
        since = time.monotonic()

        async def update(m):
            await m.update()
            await self.wait_for(m, "v2 live", lambda meta: meta.get("version") == "v2", since)
        await self.each(update)

    async def act_patch(self):
        """Build v3, which only changes a model this camera never loads, and roll
        it out right away."""
        async def patch(m):
            await m.build("v3")
            since = time.monotonic()
            m.emit("rollout", version="v3")
            await m.update()
            await self.wait_for(m, "v3 live", lambda meta: meta.get("version") == "v3", since)
        await self.each(patch)

    async def act_reset(self):
        await asyncio.gather(*(m.clean() for m in self.methods.values()))
        await methods.reset_snapshotter()
        for key in self.methods:
            self.frames[key].clear()
            self.latest.pop(key, None)
            self.link.lanes[key].bytes = 0
        self.done.clear()
        self.bus.reset()
        self.bus.emit("reset", settings=self.public_settings())

    # -- lanes ---------------------------------------------------------------

    def on_frame(self, lane, seq, jpeg, meta):
        now = time.monotonic()
        self.latest[lane] = (seq, jpeg, meta, now)
        self.frames[lane].append(now)
        self.new_frame[lane].set()
        self.bus.emit("frame", lane, seq=seq, version=meta.get("version"),
                      objects=meta.get("objects", sum(meta.get("counts", {}).values())),
                      counts=meta.get("counts"),
                      ms=meta.get("infer_ms"), models=meta.get("models"))

    async def stats_loop(self):
        last_sample = 0
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            stats = {}
            for key, m in self.methods.items():
                recent = [t for t in self.frames[key] if now - t < 2]
                latest = self.latest.get(key)
                stats[key] = {"bytes": self.link.bytes(key), "fps": round(len(recent) / 2, 1),
                              "age": round(now - latest[3], 2) if latest else None,
                              "version": latest[2].get("version") if latest else None}
            self.bus.emit("stats", source_seq=self.source.seq, lanes=stats)
            if now - last_sample >= 1:
                last_sample = now
                self.bus.trace.append({"t": self.bus.now(), "kind": "sample", "lanes": stats})


session = None


# -- helpers -------------------------------------------------------------------

def placeholder(text, sub=""):
    img = np.zeros((540, 960, 3), np.uint8)
    img[:] = (24, 24, 28)
    for i, (t, scale) in enumerate(((text, 1.1), (sub, 0.6))):
        if not t:
            continue
        (tw, th), _ = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.putText(img, t, ((960 - tw) // 2, 270 + i * 44), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (200, 200, 210), 2, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


async def mjpeg(request, get_frame):
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store"})
    await resp.prepare(request)
    try:
        while True:
            jpeg = await get_frame()
            await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                             + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


@web.middleware
async def cors_and_auth(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        if request.path.startswith("/api/") and TOKEN:
            given = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if (given or request.query.get("token")) != TOKEN:
                return web.json_response({"error": "unauthorized"}, status=401)
        if request.path.startswith("/internal/") and request.remote not in ("127.0.0.1", "::1"):
            return web.json_response({"error": "forbidden"}, status=403)
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# -- public API ------------------------------------------------------------------

async def info(request):
    s = session
    scn = s.settings["scn"]
    return web.json_response({
        "backend": {"arch": platform.machine(), "kernel": platform.release(),
                    "cpus": os.cpu_count(), "token": bool(TOKEN),
                    "gpu": methods.gpu_available()},
        "scenarios": scenario.available(),
        "scenario": {k: scn[k] for k in ("name", "title", "description", "slots",
                                         "docker_order", "select")} | {
            "frames": scn["frames"]["kind"], "enable": scn.get("enable", "segmenter"),
            "acts": scn.get("acts", {}),
            "versions": {v: {slot: spec.get("label", "")
                             for slot, spec in scenario.version_specs(scn, v).items()}
                         for v in scn["versions"]},
            "credits": {k: {"license": a["license"], "credit": a["credit"]}
                        for k, a in scn["assets"].items()}},
        "methods": [{"key": m.key, "title": m.title, "lazy": m.lazy, "partial": m.partial,
                     "refresh": m.refresh} for m in s.methods.values()],
        "settings": s.public_settings(),
        "acts": ACTS, "done": sorted(s.done), "running": s.act, "prepared": s.prepared,
    })


async def settings(request):
    s = session
    if s.act:
        return web.json_response({"error": f"act {s.act} is running"}, status=409)
    body = await request.json()
    if "device" in body:
        try:
            dev = methods.device(body["device"])
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=400)
        if dev != s.settings.get("effective_device"):
            s.prepared = False   # the lane runtime has to be rebuilt for the device
    allowed = {"scenario", "lanes", "link_mbit", "cpus", "threads", "watch_interval",
               "auto_watch", "cold_build", "mode", "device"}
    for k, v in body.items():
        if k in allowed:
            s.settings[k] = v
    if "scenario" in body and body["scenario"] != s.settings["scn"]["name"]:
        s.settings["scn"] = scenario.load(body["scenario"])
        s.prepared = False
        asyncio.create_task(s.run_act("reset"))   # the old scenario's lanes go away
    if "lanes" in body:
        s.settings["lanes"] = [k for k in s.methods if k in body["lanes"]]
    s.link.bucket.set_rate(s.settings["link_mbit"] or None)
    s.bus.emit("settings", settings=s.public_settings())
    return web.json_response(s.public_settings())


async def act(request):
    s = session
    name = request.match_info["act"]
    if name not in ACTS:
        return web.json_response({"error": f"unknown act {name}"}, status=404)
    if s.act:
        return web.json_response({"error": f"act {s.act} is running"}, status=409)
    if name not in ("prepare", "reset") and not s.prepared:
        return web.json_response({"error": "run prepare first"}, status=409)
    asyncio.create_task(s.run_act(name))
    return web.json_response({"act": name, "state": "started"}, status=202)


async def events(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-store"})
    await resp.prepare(request)
    q = session.bus.subscribe()
    try:
        await resp.write(f"data: {json.dumps({'kind': 'hello', 't': session.bus.now()})}\n\n"
                         .encode())
        while True:
            try:
                line = await asyncio.wait_for(q.get(), 15)
                await resp.write(f"data: {line}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        session.bus.unsubscribe(q)
    return resp


async def trace(request):
    s = session
    return web.json_response({"settings": s.public_settings(), "started": s.bus.wall0,
                              "events": s.bus.trace})


async def lane_video(request):
    s = session
    lane = request.match_info["lane"]
    if lane not in s.methods:
        raise web.HTTPNotFound()
    m = s.methods[lane]

    async def get_frame():
        ev = s.new_frame[lane]
        try:
            await asyncio.wait_for(ev.wait(), 1.0)
            ev.clear()
        except asyncio.TimeoutError:
            pass
        latest = s.latest.get(lane)
        if latest and time.monotonic() - latest[3] < 1.5:
            return latest[1]
        await asyncio.sleep(0.5)
        return placeholder(m.title, "no output")

    return await mjpeg(request, get_frame)


async def video_ws(request):
    """All lanes over one WebSocket. Browsers cap HTTP/1.1 connections per host
    at six, so one MJPEG stream per lane stalls once four lanes are shown."""
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    keys = list(session.methods)
    sent = {k: None for k in keys}
    try:
        while not ws.closed:
            for i, k in enumerate(keys):
                latest = session.latest.get(k)
                if latest is not None and latest is not sent[k]:
                    sent[k] = latest
                    await ws.send_bytes(bytes([i]) + latest[1])
            await asyncio.sleep(0.03)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return ws


async def source_video(request):
    seq = [0]

    async def get_frame():
        got = await session.source.next(seq[0], timeout=1.0)
        if got is None:
            return placeholder("no input")
        seq[0] = got[0]
        return got[1]

    return await mjpeg(request, get_frame)


async def camera(request):
    ws = web.WebSocketResponse(max_msg_size=4 << 20)
    await ws.prepare(request)
    session.source.mode = "camera"
    session.bus.emit("source", mode="camera")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await session.source.publish(msg.data)
    finally:
        session.source.mode = "clip"
        session.bus.emit("source", mode="clip")
    return ws


# -- internal API ------------------------------------------------------------------

async def next_frame(request):
    got = await session.source.next(int(request.query.get("after", 0)), timeout=2.0)
    if got is None:
        return web.Response(status=204)
    return web.Response(body=got[1], content_type="image/jpeg",
                        headers={"X-Seq": str(got[0])})


async def lane_frame(request):
    lane = request.match_info["lane"]
    jpeg = await request.read()
    meta = json.loads(request.headers.get("X-Meta", "{}"))
    session.on_frame(lane, int(request.headers.get("X-Seq", 0)), jpeg, meta)
    return web.Response(status=204)


async def lane_event(request):
    lane = request.match_info["lane"]
    data = await request.json()
    session.bus.emit("app", lane, **data)
    return web.Response(status=204)


async def index(request):
    return web.FileResponse(os.path.join(DASHBOARD, "index.html"))


async def main():
    global session
    session = Session()
    await session.start()
    app = web.Application(middlewares=[cors_and_auth], client_max_size=8 << 20)
    app.add_routes([
        web.get("/api/info", info),
        web.post("/api/settings", settings),
        web.post("/api/acts/{act}", act),
        web.get("/api/events", events),
        web.get("/api/trace", trace),
        web.get("/api/video", video_ws),
        web.get("/api/lanes/{lane}/video", lane_video),
        web.get("/api/source/video", source_video),
        web.get("/api/camera", camera),
        web.get("/internal/frames/next", next_frame),
        web.post("/internal/lanes/{lane}/frame", lane_frame),
        web.post("/internal/lanes/{lane}/event", lane_event),
        web.get("/", index),
    ])
    if os.path.isdir(DASHBOARD):
        app.router.add_static("/", DASHBOARD)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("DEMO_PORT", "8080"))).start()
    print("controller listening on :8080", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
