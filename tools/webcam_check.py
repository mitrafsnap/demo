"""Check the webcam path with Chromium's fake camera (a moving test pattern).

    docker run --rm -v "$PWD/tools:/tools" -v "$OUT:/out" \
      mcr.microsoft.com/playwright/python:v1.55.0-noble \
      python3 /tools/webcam_check.py http://host.docker.internal:8080

The page is opened on localhost (forwarded to the backend), because browsers
only allow the camera on secure origins. Writes /out/fallback.png (before) and /out/webcam.png (after clicking the button).
"""

import asyncio
import sys
import threading
import urllib.parse

from playwright.sync_api import sync_playwright


def forward(listen_port, host, port):
    """localhost:listen_port -> host:port, so the page has a secure origin."""
    async def pipe(r, w):
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
        w.close()

    async def handle(r, w):
        ur, uw = await asyncio.open_connection(host, port)
        await asyncio.gather(pipe(r, uw), pipe(ur, w))

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
        await server.serve_forever()

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()


target = urllib.parse.urlparse(sys.argv[1])
forward(8080, target.hostname, target.port or 80)
base = "http://localhost:8080"
with sync_playwright() as p:
    browser = p.chromium.launch(args=[
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, permissions=["camera"])
    page = ctx.new_page()
    page.on("pageerror", lambda e: print("pageerror:", e))
    page.goto(f"{base}/?backend={base}")
    page.wait_for_selector("#status.ok", timeout=30_000)
    page.wait_for_timeout(3000)
    page.screenshot(path="/out/fallback.png")
    page.click("#webcam")
    page.wait_for_timeout(5000)
    print("button:", page.inner_text("#webcam"))
    print("log:", page.inner_text("#log")[:400])
    print("secure:", page.evaluate("window.isSecureContext"),
          "mediaDevices:", page.evaluate("!!navigator.mediaDevices"))
    page.screenshot(path="/out/webcam.png")
    page.click("#webcam")
    browser.close()
