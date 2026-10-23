"""Emulated network link between the registry and the lanes.

Each lane pulls through its own local port, so its bytes are counted
separately, while all lanes draw from one shared token bucket like a real
uplink. Builders push to the registry directly and are not limited.
"""

import asyncio
import time


class Bucket:
    """Token bucket shared by every connection. rate=None means unlimited."""

    def __init__(self, mbit):
        self.set_rate(mbit)

    def set_rate(self, mbit):
        self.mbit = mbit
        self.rate = mbit * 125_000 if mbit else None
        self.tokens = 0.0
        self.stamp = time.monotonic()

    async def take(self, n):
        if self.rate is None:
            return
        while True:
            now = time.monotonic()
            # Allow ~50 ms of burst so short pulls can't overshoot the rate, but
            # always enough for one read.
            burst = max(self.rate * 0.05, 128 * 1024)
            self.tokens = min(burst, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens >= n:
                self.tokens -= n
                return
            await asyncio.sleep((n - self.tokens) / self.rate)


class LaneLink:
    def __init__(self, lane, port, bucket, upstream):
        self.lane, self.port, self.bucket, self.upstream = lane, port, bucket, upstream
        self.bytes = 0
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)

    async def handle(self, reader, writer):
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.upstream)
        except OSError:
            writer.close()
            return

        async def pipe(src, dst, limited):
            try:
                while data := await src.read(64 * 1024):
                    if limited:
                        await self.bucket.take(len(data))
                        self.bytes += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_writer, False), pipe(up_reader, writer, True))


class Link:
    def __init__(self, upstream, mbit):
        self.upstream = upstream
        self.bucket = Bucket(mbit)
        self.lanes = {}

    async def add(self, lane, port):
        link = LaneLink(lane, port, self.bucket, self.upstream)
        await link.start()
        self.lanes[lane] = link
        return link

    def host(self, lane):
        return f"127.0.0.1:{self.lanes[lane].port}"

    def bytes(self, lane):
        return self.lanes[lane].bytes
