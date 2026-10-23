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
        return [(COCO[cls[i]], float(conf[i]), boxes[i]) for i in np.array(idx).flatten()]


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


KINDS = {"yolox": YoloxDetector, "voc-seg": VocSegmenter, "imagenet-cls": Classifier}


def load(path, spec, threads):
    t0 = time.perf_counter()
    model = KINDS[spec["kind"]](path, spec, threads)
    return model, time.perf_counter() - t0


def draw(frame, detections, mask, seg_classes, label):
    out = frame.copy()
    if mask is not None:
        overlay = np.zeros_like(out)
        for c in seg_classes:
            overlay[mask == VOC.index(c)] = color(c)
        sel = overlay.any(2)
        out[sel] = cv2.addWeighted(out, 0.45, overlay, 0.55, 0)[sel]
    for name, conf, (x, y, w, h) in detections:
        c = color(name)
        p1, p2 = (int(x), int(y)), (int(x + w), int(y + h))
        cv2.rectangle(out, p1, p2, c, 2)
        txt = f"{name} {conf:.2f}"
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.rectangle(out, (p1[0], p1[1] - th - 6), (p1[0] + tw + 4, p1[1]), c, -1)
        cv2.putText(out, txt, (p1[0] + 2, p1[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 0, 0), 1, cv2.LINE_AA)
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
