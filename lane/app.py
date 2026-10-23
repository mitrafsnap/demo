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
        enabled = os.environ.get("ENABLED")
        self.enabled = (set(enabled.split(",")) if enabled else
                        {k for k, v in self.config["models"].items() if v.get("enabled")})
        self.models = {}    # name -> (model, digest)
        self.mask = None
        self.frame = None   # latest frame, for the segmentation loop

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
                fresh, seconds = self.load_missing(config, "update")
                self.config_digest = digest
                self.mask = None
                self.swap(config, fresh)
            event(client, "reload_done", version=config["version"], models=sorted(fresh),
                  seconds=round(seconds, 3))

    def segment_loop(self):
        """Segment the latest frame whenever the segmenter is free. It is much
        slower than detection, so frames keep flowing with the last mask."""
        while True:
            seg, frame = self.models.get("segmenter"), self.frame
            if seg is None or frame is None:
                time.sleep(0.05)
                continue
            try:
                self.mask = seg[0](frame)
            except (OSError, EOFError):
                time.sleep(0.05)  # worker replaced mid-call

    def run(self):
        threading.Thread(target=self.watch_config, daemon=True).start()
        threading.Thread(target=self.segment_loop, daemon=True).start()
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
            det, seg = models.get("detector"), models.get("segmenter")
            self.frame = frame
            try:
                detections = det[0](frame) if det else []
            except (OSError, EOFError):
                detections = []   # worker replaced mid-call
            mask = self.mask if seg else None
            infer_ms = (time.perf_counter() - t0) * 1000
            seg_classes = config["models"].get("segmenter", {}).get("classes", [])
            label = f"{config['version']}  {' + '.join(sorted(models))}"
            out = vision.draw(frame, detections, mask, seg_classes, label)
            ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 75])
            counts = {}
            for name, _, _ in detections:
                counts[name] = counts.get(name, 0) + 1
            meta = {"version": config["version"], "infer_ms": round(infer_ms, 1),
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
