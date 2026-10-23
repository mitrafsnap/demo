"""Model wrappers used by the lane app. Each wrapper loads one ONNX file and
turns a BGR frame into drawable results."""

import time

import cv2
import numpy as np
import onnxruntime as ort

COCO = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]

VOC = [
    "background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car", "cat",
    "chair", "cow", "diningtable", "dog", "horse", "motorbike", "person", "pottedplant",
    "sheep", "sofa", "train", "tvmonitor",
]


def session(path, threads):
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                 if p in ort.get_available_providers()]
    return ort.InferenceSession(path, opts, providers=providers)


def color(name):
    h = sum(ord(c) * 31 ** i for i, c in enumerate(name)) % 180
    hsv = np.uint8([[[h, 200, 255]]])
    return tuple(int(c) for c in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0])


class YoloxDetector:
    """YOLOX ONNX from the official releases: raw BGR input, letterboxed with 114,
    outputs before grid decoding."""

    def __init__(self, path, spec, threads):
        self.sess = session(path, threads)
        self.size = int(spec.get("input", 416))
        self.score = float(spec.get("score", 0.35))
        self.classes = {COCO.index(c) for c in spec["classes"]}
        self.input = self.sess.get_inputs()[0].name
        self._grids = self._make_grids()

    def _make_grids(self):
        grids, strides = [], []
        for s in (8, 16, 32):
            n = self.size // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        return np.concatenate(grids), np.concatenate(strides)

    def __call__(self, frame):
        h, w = frame.shape[:2]
        r = min(self.size / h, self.size / w)
        img = np.full((self.size, self.size, 3), 114, np.uint8)
        img[: int(h * r), : int(w * r)] = cv2.resize(frame, (int(w * r), int(h * r)))
        blob = img.transpose(2, 0, 1)[None].astype(np.float32)
        out = self.sess.run(None, {self.input: blob})[0][0]
        grids, strides = self._grids
        xy = (out[:, :2] + grids) * strides
        wh = np.exp(out[:, 2:4]) * strides
        scores = out[:, 4:5] * out[:, 5:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(cls)), cls]
        keep = (conf > self.score) & np.isin(cls, list(self.classes))
        xy, wh, cls, conf = xy[keep], wh[keep], cls[keep], conf[keep]
        boxes = np.concatenate([xy - wh / 2, wh], 1) / r
        idx = cv2.dnn.NMSBoxes(boxes.tolist(), conf.tolist(), self.score, 0.45)
        return [{"name": COCO[cls[i]], "conf": float(conf[i]), "box": boxes[i]}
                for i in np.array(idx).flatten()]


class VocSegmenter:
    """FCN from the ONNX model zoo: ImageNet-normalized RGB in, 21 VOC class logits
    out. Runs at a reduced resolution; the mask is upscaled for drawing."""

    MEAN = np.array([0.485, 0.456, 0.406], np.float32)
    STD = np.array([0.229, 0.224, 0.225], np.float32)

    def __init__(self, path, spec, threads):
        self.sess = session(path, threads)
        self.width = int(spec.get("width", 384))
        self.classes = [VOC.index(c) for c in spec["classes"]]
        self.input = self.sess.get_inputs()[0].name

    def __call__(self, frame):
        h, w = frame.shape[:2]
        sw, sh = self.width, int(self.width * h / w) // 8 * 8
        img = cv2.cvtColor(cv2.resize(frame, (sw, sh)), cv2.COLOR_BGR2RGB)
        blob = ((img / 255.0 - self.MEAN) / self.STD).transpose(2, 0, 1)[None].astype(np.float32)
        labels = self.sess.run(None, {self.input: blob})[0][0].argmax(0).astype(np.uint8)
        return cv2.resize(labels, (w, h), interpolation=cv2.INTER_NEAREST)


class Classifier:
    """Plain ImageNet classifier. Only loaded if the presenter enables it."""

    def __init__(self, path, spec, threads):
        self.sess = session(path, threads)
        self.input = self.sess.get_inputs()[0].name

    def __call__(self, frame):
        img = cv2.cvtColor(cv2.resize(frame, (224, 224)), cv2.COLOR_BGR2RGB)
        blob = ((img / 255.0 - VocSegmenter.MEAN) / VocSegmenter.STD).transpose(2, 0, 1)
        out = self.sess.run(None, {self.input: blob[None].astype(np.float32)})[0]
        return int(out.argmax())


class FaceDetector:
    """YuNet from the OpenCV model zoo, run through OpenCV's FaceDetectorYN."""

    def __init__(self, path, spec, threads):
        cv2.setNumThreads(threads)
        self.score = float(spec.get("score", 0.7))
        self.det = cv2.FaceDetectorYN.create(path, "", (320, 320), self.score, 0.3, 50)
        self.size = None

    def __call__(self, frame):
        h, w = frame.shape[:2]
        if self.size != (w, h):
            self.det.setInputSize((w, h))
            self.size = (w, h)
        _, faces = self.det.detect(frame)
        if faces is None:
            return []
        return [{"name": "face", "conf": float(f[14]), "box": f[:4],
                 "points": f[4:14].reshape(5, 2)} for f in faces]


class Expression:
    """FER+ from the ONNX model zoo: one 64x64 grayscale face in, 8 emotions out.
    Runs on the faces the detector found."""

    LABELS = ["neutral", "happy", "surprised", "sad", "angry", "disgusted", "afraid",
              "contempt"]

    def __init__(self, path, spec, threads):
        self.sess = session(path, threads)
        self.input = self.sess.get_inputs()[0].name

    def __call__(self, payload):
        frame, boxes = payload
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out = []
        for x, y, w, h in boxes:
            x0, y0 = max(0, int(x)), max(0, int(y))
            crop = gray[y0:int(y + h), x0:int(x + w)]
            if crop.size == 0:
                out.append(None)
                continue
            blob = cv2.resize(crop, (64, 64)).astype(np.float32)[None, None]
            logits = self.sess.run(None, {self.input: blob})[0][0]
            p = np.exp(logits - logits.max())
            p /= p.sum()
            out.append((self.LABELS[int(p.argmax())], float(p.max())))
        return out


class Pose:
    """RTMPose-m (SimCC) top-down body keypoints. A webcam shows people from the
    waist up, so each person box is derived from a face box."""

    MEAN = np.array([123.675, 116.28, 103.53], np.float32)
    STD = np.array([58.395, 57.12, 57.375], np.float32)
    W, H = 192, 256

    def __init__(self, path, spec, threads):
        self.sess = session(path, threads)
        self.input = self.sess.get_inputs()[0].name

    def person_box(self, face, shape):
        x, y, w, h = face
        cx, top = x + w / 2, y - 0.6 * h
        bw = 4.0 * w
        bh = min(shape[0] - top, 6.5 * h)
        return cx, top + bh / 2, bw, bh

    def __call__(self, payload):
        frame, faces = payload
        boxes = [self.person_box(f, frame.shape) for f in faces] or [
            (frame.shape[1] / 2, frame.shape[0] / 2, frame.shape[1], frame.shape[0])]
        people = []
        for cx, cy, bw, bh in boxes[:3]:
            # pad 1.25 and match the model's aspect ratio
            bw, bh = bw * 1.25, bh * 1.25
            if bw / bh > self.W / self.H:
                bh = bw * self.H / self.W
            else:
                bw = bh * self.W / self.H
            src = np.float32([[cx - bw / 2, cy - bh / 2], [cx + bw / 2, cy - bh / 2],
                              [cx - bw / 2, cy + bh / 2]])
            dst = np.float32([[0, 0], [self.W, 0], [0, self.H]])
            crop = cv2.warpAffine(frame, cv2.getAffineTransform(src, dst), (self.W, self.H))
            img = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32)
            blob = ((img - self.MEAN) / self.STD).transpose(2, 0, 1)[None]
            sx, sy = self.sess.run(None, {self.input: blob})
            kx, ky = sx[0].argmax(1) / 2.0, sy[0].argmax(1) / 2.0
            score = np.minimum(sx[0].max(1), sy[0].max(1))
            pts = np.stack([cx - bw / 2 + kx * bw / self.W, cy - bh / 2 + ky * bh / self.H], 1)
            people.append((pts, score))
        return people


KINDS = {"yolox": YoloxDetector, "voc-seg": VocSegmenter, "imagenet-cls": Classifier,
         "yunet": FaceDetector, "ferplus": Expression, "rtmpose": Pose}

# COCO keypoint pairs drawn as a skeleton
SKELETON = [(5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12),
            (11, 13), (13, 15), (12, 14), (14, 16), (0, 1), (0, 2), (1, 3), (2, 4)]


def load(path, spec, threads):
    t0 = time.perf_counter()
    model = KINDS[spec["kind"]](path, spec, threads)
    return model, time.perf_counter() - t0


def _label(out, text, p, c):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
    cv2.rectangle(out, (p[0], p[1] - th - 6), (p[0] + tw + 4, p[1]), c, -1)
    cv2.putText(out, text, (p[0] + 2, p[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1,
                cv2.LINE_AA)


def draw(frame, detections, overlays, label):
    """detections: dicts with name, conf, box and optional points and tag.
    overlays: {"mask": (labels, classes)} and/or {"pose": [(points, scores)]}."""
    out = frame.copy()
    if "mask" in overlays:
        mask, classes = overlays["mask"]
        layer = np.zeros_like(out)
        for c in classes:
            layer[mask == VOC.index(c)] = color(c)
        sel = layer.any(2)
        out[sel] = cv2.addWeighted(out, 0.45, layer, 0.55, 0)[sel]
    for pts, score in overlays.get("pose", []):
        c = color("pose")
        for a, b in SKELETON:
            if score[a] > 0.3 and score[b] > 0.3:
                cv2.line(out, tuple(int(v) for v in pts[a]), tuple(int(v) for v in pts[b]), c, 3,
                         cv2.LINE_AA)
        for (x, y), sc in zip(pts, score):
            if sc > 0.3:
                cv2.circle(out, (int(x), int(y)), 4, (255, 255, 255), -1, cv2.LINE_AA)
    for d in detections:
        x, y, w, h = d["box"]
        c = color(d["name"])
        p1, p2 = (int(x), int(y)), (int(x + w), int(y + h))
        cv2.rectangle(out, p1, p2, c, 2)
        for px, py in d.get("points", []):
            cv2.circle(out, (int(px), int(py)), 2, c, -1)
        _label(out, d.get("tag") or f"{d['name']} {d['conf']:.2f}", p1, c)
    cv2.rectangle(out, (0, 0), (out.shape[1], 26), (0, 0, 0), -1)
    cv2.putText(out, label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                cv2.LINE_AA)
    return out


def _serve(conn, path, spec, threads):
    """Worker process body: load one model, then answer frames until told to stop."""
    try:
        model, seconds = load(path, spec, threads)
    except Exception as e:  # report instead of dying silently
        conn.send(("error", repr(e)))
        return
    conn.send(("ready", seconds))
    while (frame := conn.recv()) is not None:
        conn.send(model(frame))


class Worker:
    """One model in its own process. Loading a model holds the GIL for its whole
    duration, so a separate process is what lets the app keep serving frames
    with the old model while the new one loads."""

    def __init__(self, path, spec, threads):
        import multiprocessing as mp
        import threading
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_serve, args=(child, path, spec, threads), daemon=True)
        self.proc.start()
        status, value = self.conn.recv()
        if status != "ready":
            self.proc.join(1)
            raise RuntimeError(value)
        self.seconds = value
        self.lock = threading.Lock()

    def __call__(self, frame):
        with self.lock:
            self.conn.send(frame)
            return self.conn.recv()

    def close(self):
        try:
            with self.lock:
                self.conn.send(None)
        except OSError:
            pass
        self.proc.join(2)
        if self.proc.is_alive():
            self.proc.kill()
