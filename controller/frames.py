"""Frame source shared by all lanes, so every method sees the same frames.

A clip is decoded once into JPEGs and played in a loop at a fixed rate. In
camera mode, frames come from the dashboard browser instead.
"""

import asyncio

import cv2


def decode_clip(path, fps, width, start=0, end=None):
    cap = cv2.VideoCapture(path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = src_fps / fps
    first, last = start * src_fps, end * src_fps if end else float("inf")
    frames, i, next_pick = [], 0, first
    while True:
        ok, frame = cap.read()
        if not ok or i > last:
            break
        if i >= next_pick:
            h, w = frame.shape[:2]
            frame = cv2.resize(frame, (width, int(h * width / w) // 2 * 2))
            frames.append(cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes())
            next_pick += step
        i += 1
    cap.release()
    return frames


class FrameSource:
    def __init__(self):
        self.seq = 0
        self.jpeg = None
        self.cond = asyncio.Condition()
        self.clip = []
        self.fps = 10
        self.mode = "clip"
        self.task = None

    def load_clip(self, frames, fps):
        self.clip, self.fps = frames, fps

    async def publish(self, jpeg):
        async with self.cond:
            self.seq += 1
            self.jpeg = jpeg
            self.cond.notify_all()

    async def play(self):
        i = 0
        loop = asyncio.get_running_loop()
        tick = loop.time()
        while True:
            if self.mode == "clip" and self.clip:
                await self.publish(self.clip[i % len(self.clip)])
                i += 1
            tick += 1 / self.fps
            await asyncio.sleep(max(0, tick - loop.time()))

    def start(self):
        self.task = asyncio.create_task(self.play())

    async def next(self, after, timeout=2.0):
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(lambda: self.seq > after), timeout)
            except asyncio.TimeoutError:
                return None
            return self.seq, self.jpeg
