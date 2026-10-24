"""Network link between the registry and the lanes.

Each lane pulls through its own local port, so its bytes are counted
separately. When the registry runs on this machine, all lanes also draw from
one shared token bucket, emulating an edge uplink; when it runs elsewhere, the
real network is the link and nothing is throttled. Builders push to the
registry directly and are not limited.
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
    def __init__(self, lane, port, link):
        self.lane, self.port, self.link = lane, port, link
        self.bytes = 0
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)

    async def handle(self, reader, writer):
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.link.upstream)
        except OSError:
            writer.close()
            return

        async def pipe(src, dst, limited):
            try:
                while data := await src.read(64 * 1024):
                    if limited:
                        await self.link.bucket.take(len(data))
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
        link = LaneLink(lane, port, self)
        await link.start()
        self.lanes[lane] = link
        return link

    def host(self, lane):
        return f"127.0.0.1:{self.lanes[lane].port}"

    def bytes(self, lane):
        return self.lanes[lane].bytes
