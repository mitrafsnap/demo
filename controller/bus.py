"""Event bus. Every event is timestamped relative to the run start, kept in
the run's trace, and fanned out to server-sent-event subscribers."""

import asyncio
import json
import time


class Bus:
    def __init__(self):
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.trace = []
        self.subscribers = set()

    def reset(self):
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.trace = []

    def now(self):
        return round(time.monotonic() - self.t0, 3)

    def emit(self, kind, lane=None, **data):
        ev = {"t": self.now(), "kind": kind, "lane": lane, **data}
        if kind != "stats":
            self.trace.append(ev)
        line = json.dumps(ev)
        for q in list(self.subscribers):
            if q.qsize() < 1000:
                q.put_nowait(line)
        return ev

    def subscribe(self):
        q = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        self.subscribers.discard(q)
