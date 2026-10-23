"""Lane app: one per method. Reads frames from the controller, runs the enabled
models from /models, and sends annotated frames and events back.

The app never learns how its files arrived. It reopens /models/config.json a
few times per second and reloads any model whose digest changed, which is how
it picks up an in place refresh. Replacement lanes see the new version when
they are restarted instead.
"""

import hashlib
import http.client
import http.server
import json
import os
import threading
import time
import urllib.parse

import cv2
import numpy as np

import vision

LANE = os.environ["LANE"]
CONTROLLER = urllib.parse.urlparse(os.environ.get("CONTROLLER", "http://127.0.0.1:8080"))
PORT = int(os.environ.get("PORT", "8100"))
MODELS = os.environ.get("MODELS_DIR", "/models")
THREADS = int(os.environ.get("THREADS", "2"))
CONFIG = os.path.join(MODELS, "config.json")

# How each model kind is used per frame: detectors run on every frame,
# per-detection models on each detected box, async models in the background on
# the latest frame (their last result is drawn until the next one is ready).
ROLES = {"yolox": "detect", "yunet": "detect", "ferplus": "per-detection",
         "voc-seg": "async", "boxsam": "async", "rtmpose": "async", "imagenet-cls": "async"}


def role(spec):
    return spec.get("role") or ROLES[spec["kind"]]


class Controller:
    """Keep-alive HTTP client to the controller, one per thread."""

    def __init__(self):
        self.conn = None

    def request(self, method, path, body=None, headers=None, timeout=10):
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(
                        CONTROLLER.hostname, CONTROLLER.port, timeout=timeout)
                self.conn.request(method, path, body=body, headers=headers or {})
                resp = self.conn.getresponse()
                return resp.status, dict(resp.getheaders()), resp.read()
            except (OSError, http.client.HTTPException):
                self.conn = None
                if attempt:
                    raise


def event(client, phase, **data):
    body = json.dumps({"phase": phase, **data}).encode()
    try:
        client.request("POST", f"/internal/lanes/{LANE}/event", body,
                       {"Content-Type": "application/json"})
    except OSError:
        pass


class Lane:
    def __init__(self):
        self.lock = threading.Lock()
        self.client = Controller()
        self.config_digest, self.config = self.read_config()
        # Models the deployed version turns on, plus any the presenter enabled.
        self.enabled = set(filter(None, os.environ.get("ENABLED", "").split(","))) | \
            {k for k, v in self.config["models"].items() if v.get("enabled")}
        self.models = {}    # name -> (model, digest)
        self.results = {}   # async model -> last result
        self.frame = None   # latest (frame, detections), for the async loop

    def read_config(self):
        with open(CONFIG, "rb") as f:
            raw = f.read()
        return hashlib.sha256(raw).hexdigest(), json.loads(raw)

    def load_missing(self, config, reason):
        """Load enabled models whose digest differs from the loaded one, without
        blocking the frame loop. On a lazy mount this is what fetches them."""
        t0 = time.perf_counter()
        fresh = {}
        for name in sorted(self.enabled):
            spec = config["models"][name]
            have = self.models.get(name)
            if have and have[1] == spec["sha256"]:
                continue
            path = os.path.join(MODELS, spec["file"])
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            event(self.client, "load_start", model=name, reason=reason)
            t1 = time.perf_counter()
            worker = vision.Worker(path, spec, THREADS)
            fresh[name] = (worker, spec["sha256"])
            event(self.client, "load_done", model=name,
                  seconds=round(time.perf_counter() - t1, 3), version=config["version"])
        return fresh, time.perf_counter() - t0

    def swap(self, config, fresh):
        old = self.models
        models = {k: v for k, v in old.items() if k in self.enabled}
        models.update(fresh)
        self.config, self.models = config, models
        for name, (worker, _) in old.items():
            if models.get(name, (None,))[0] is not worker:
                threading.Thread(target=worker.close, daemon=True).start()

    def enable(self, name):
        with self.lock:
            if name not in self.config["models"]:
                return 404, f"unknown model {name}"
            self.enabled.add(name)
            try:
                fresh, _ = self.load_missing(self.config, "enable")
            except FileNotFoundError:
                self.enabled.discard(name)
                event(self.client, "missing", model=name)
                return 409, f"{name} is not in this container's image"
            self.swap(self.config, fresh)
        return 200, "ok"

    def watch_config(self):
        """Reopen config.json; a new digest means new files behind the mount."""
        client = Controller()
        while True:
            time.sleep(0.2)
            try:
                digest, config = self.read_config()
            except (OSError, ValueError):
                continue
            if digest == self.config_digest:
                continue
            event(client, "reload_start", version=config["version"])
            with self.lock:
                self.enabled |= {k for k, v in config["models"].items() if v.get("enabled")}
                fresh, seconds = self.load_missing(config, "update")
                self.config_digest = digest
                self.results = {}
                self.swap(config, fresh)
            event(client, "reload_done", version=config["version"], models=sorted(fresh),
                  seconds=round(seconds, 3))

    def async_loop(self):
        """Run the async models on the latest frame whenever they are free. They
        are slower than detection, so frames keep flowing with the last result."""
        while True:
            latest = self.frame
            jobs = [(n, m) for n, m in self.models.items()
                    if role(self.config["models"][n]) == "async"]
            if latest is None or not jobs:
                time.sleep(0.05)
                continue
            frame, dets = latest
            for name, (worker, _) in jobs:
                kind = self.config["models"][name]["kind"]
                payload = {"rtmpose": (frame, [d["box"] for d in dets]),
                           "boxsam": (frame, dets)}.get(kind, frame)
                try:
                    self.results[name] = worker(payload)
                except (OSError, EOFError):
                    time.sleep(0.05)  # worker replaced mid-call

    def infer(self, frame, config, models):
        """Detections with per-detection tags, plus overlays from async models."""
        specs = config["models"]
        dets = []
        for name, (worker, _) in models.items():
            if role(specs[name]) == "detect":
                dets += worker(frame)
        boxes = [d["box"] for d in dets]
        for name, (worker, _) in models.items():
            if role(specs[name]) == "per-detection" and boxes:
                for d, tag in zip(dets, worker((frame, boxes))):
                    if tag:
                        d["tag"] = f"{tag[0]} {tag[1]:.2f}"
        self.frame = (frame, dets)
        overlays = {}
        for name in models:
            result = self.results.get(name)
            if result is None:
                continue
            kind = specs[name]["kind"]
            if kind == "voc-seg":
                overlays["mask"] = (result, specs[name].get("classes", []))
            elif kind == "boxsam":
                overlays["instances"] = vision.place_instances(dets, result)
            elif kind == "rtmpose":
                overlays["pose"] = result
        return dets, overlays

    def run(self):
        threading.Thread(target=self.watch_config, daemon=True).start()
        threading.Thread(target=self.async_loop, daemon=True).start()
        with self.lock:
            fresh, _ = self.load_missing(self.config, "start")
            self.swap(self.config, fresh)
        event(self.client, "serving", version=self.config["version"],
              models=sorted(self.enabled))
        seq = 0
        while True:
            status, headers, body = self.client.request(
                "GET", f"/internal/frames/next?after={seq}&lane={LANE}")
            if status != 200:
                continue
            seq = int(headers.get("X-Seq", seq + 1))
            frame = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
            t0 = time.perf_counter()
            config, models = self.config, self.models
            try:
                detections, overlays = self.infer(frame, config, models)
            except (OSError, EOFError):
                detections, overlays = [], {}   # worker replaced mid-call
            infer_ms = (time.perf_counter() - t0) * 1000
            label = f"{config['version']}  {' + '.join(sorted(models))}"
            out = vision.draw(frame, detections, overlays, label)
            ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 75])
            counts = {}
            for d in detections:
                for key in (d["name"], d.get("tag", "").split(" ")[0]):
                    if key:
                        counts[key] = counts.get(key, 0) + 1
            meta = {"version": config["version"], "infer_ms": round(infer_ms, 1),
                    "objects": len(detections),
                    "counts": counts, "models": sorted(models)}
            self.client.request("POST", f"/internal/lanes/{LANE}/frame", jpg.tobytes(),
                                {"Content-Type": "image/jpeg", "X-Seq": str(seq),
                                 "X-Meta": json.dumps(meta)})


def serve_control(lane):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.path.startswith("/enable"):
                code, msg = lane.enable(q.get("model", [""])[0])
            else:
                code, msg = 404, "not found"
            self.send_response(code)
            self.end_headers()
            self.wfile.write(msg.encode())

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"lane": LANE, "enabled": sorted(lane.enabled),
                                         "version": lane.config["version"]}).encode())

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    lane = Lane()
    serve_control(lane)
    event(lane.client, "app_start")
    lane.run()
