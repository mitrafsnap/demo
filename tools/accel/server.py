"""Apple GPU helper: runs lane models with ONNX Runtime's CoreML provider on the
Mac itself, because Docker Desktop's Linux VM has no GPU.

    tools/accel/run.sh          (creates a venv and starts this on 127.0.0.1:8090)

The lanes still read every model file from their own container mount; they
send the bytes here once per model version (keyed by digest) and then send
input tensors per frame. A model CoreML cannot compile runs on the CPU here.
Each model lives in its own process: compiling one holds the GIL for seconds,
which must not stall the others' inference.
"""

import argparse
import hashlib
import io
import json
import multiprocessing as mp
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort

COREML = ("CoreMLExecutionProvider", {"MLComputeUnits": "ALL", "ModelFormat": "MLProgram"})
models = {}      # digest -> Model
lock = threading.Lock()


DTYPES = {"tensor(float)": np.float32, "tensor(float16)": np.float16,
          "tensor(int64)": np.int64, "tensor(int32)": np.int32, "tensor(uint8)": np.uint8}


def warm_up(sess):
    """CoreML specializes a model on its first run, which can take a second;
    do that before the lane switches to the model, not on its first frame."""
    feeds = {i.name: np.zeros([d if isinstance(d, int) else 1 for d in i.shape],
                              DTYPES.get(i.type, np.float32)) for i in sess.get_inputs()}
    try:
        sess.run(None, feeds)
    except Exception:   # zeros aren't valid for every model; then skip it
        pass


def serve(conn, path, threads):
    """Model process: compile, report, then answer npz requests."""
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    opts.intra_op_num_threads = threads
    try:
        sess, provider = ort.InferenceSession(data, opts, providers=[COREML, "CPUExecutionProvider"]), "CoreML"
    except Exception:   # e.g. unbounded dimensions CoreML can't plan
        sess, provider = ort.InferenceSession(data, opts, providers=["CPUExecutionProvider"]), "CPU"
    warm_up(sess)
    conn.send({"provider": provider,
               "inputs": [{"name": i.name, "shape": i.shape, "type": i.type} for i in sess.get_inputs()],
               "outputs": [o.name for o in sess.get_outputs()]})
    while (body := conn.recv_bytes()):
        with np.load(io.BytesIO(body)) as z:
            feeds = {k: z[k].astype(np.float32) if z[k].dtype == np.float16 else z[k] for k in z}
        buf = io.BytesIO()
        np.savez(buf, *sess.run(None, feeds))
        conn.send_bytes(buf.getvalue())


class Model:
    def __init__(self, data, digest, threads):
        # pass a file, not the bytes: pickling a large model holds the GIL
        path = os.path.join(tempfile.gettempdir(), f"accel-{digest}.onnx")
        with open(path, "wb") as f:
            f.write(data)
        self.conn, child = mp.Pipe()
        self.proc = mp.get_context("spawn").Process(target=serve, args=(child, path, threads), daemon=True)
        self.proc.start()
        self.info = self.conn.recv()
        self.lock = threading.Lock()

    def run(self, body):
        with self.lock:
            self.conn.send_bytes(body)
            return self.conn.recv_bytes()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, json.dumps({"providers": ort.get_available_providers(),
                                               "models": len(models)}).encode())
        digest = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/models/") and digest in models:
            return self.reply(200, json.dumps(models[digest].info).encode())
        self.reply(404, b"{}")

    def do_POST(self):
        if self.path == "/models":
            data = self.body()
            digest = hashlib.sha256(data).hexdigest()
            with lock:   # only serializes loading the same model twice; compiling runs elsewhere
                if digest not in models:
                    t0 = time.perf_counter()
                    models[digest] = Model(data, digest, ARGS.threads)
                    print(f"loaded {digest[:12]} on {models[digest].info['provider']} "
                          f"in {time.perf_counter() - t0:.1f}s ({len(data) / 1e6:.0f} MB)", flush=True)
            return self.reply(200, json.dumps({"digest": digest, **models[digest].info}).encode())
        if self.path.startswith("/run/"):
            out = models[self.path.rsplit("/", 1)[-1]].run(self.body())
            return self.reply(200, out, "application/octet-stream")
        self.reply(404, b"{}")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--threads", type=int, default=4, help="CPU threads for models CoreML can't run")
    ARGS = ap.parse_args()
    print(f"Apple GPU helper on 127.0.0.1:{ARGS.port}, providers {ort.get_available_providers()}",
          flush=True)
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler).serve_forever()
