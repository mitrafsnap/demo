"""Record the dashboard while the acts run, as high-quality frames.

    docker run --rm -v "$PWD/tools:/tools" -v "$OUT:/out" \
      mcr.microsoft.com/playwright/python:v1.55.0-noble \
      python3 /tools/record.py http://host.docker.internal:8080 [--lanes all] [--scenario traffic]

Writes /out/frames/*.jpg and /out/frames.txt, an ffmpeg concat list with each
frame's real duration. tools/encode.sh turns them into the MP4 and GIF.
Playwright's own video recorder has a fixed low bitrate, hence screenshots.
"""

import argparse
import json
import os
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright

ACTS = ["prepare", "reset", "build-v1", "deploy", "add-model", "build-v3", "rollout-v3", "patch"]

ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("--lanes", default="all")
ap.add_argument("--scenario", default="traffic")
ap.add_argument("--width", type=int, default=1920)
ap.add_argument("--height", type=int, default=1080)
ap.add_argument("--hold", type=float, default=4, help="seconds to show each act's result")
ap.add_argument("--speed", default="2", help="playback speed the intro card announces")
ap.add_argument("--check", action="store_true", help="one screenshot, no acts")
args = ap.parse_args()
LANES = ["docker", "estargz", "twodfs", "mitrafsnap"] if args.lanes == "all" else args.lanes.split(",")


def call(path, body=None):
    req = urllib.request.Request(args.base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def run_acts(done):
    try:
        for act in ACTS:
            call(f"/api/acts/{act}", {})
            time.sleep(1)
            while True:
                info = call("/api/info")
                if info["running"] is None:
                    break
                time.sleep(0.5)
            if act != "reset" and act not in info["done"]:
                print("act failed:", act, flush=True)
                break
            print("done", act, flush=True)
            time.sleep(args.hold)
        time.sleep(args.hold)
    finally:
        done.set()


call("/api/settings", {"scenario": args.scenario})
time.sleep(2)
call("/api/settings", {"lanes": LANES})

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": args.width, "height": args.height})
    page.on("pageerror", lambda e: print("pageerror:", e))
    if not args.check:
        os.makedirs("/out/frames", exist_ok=True)
        for slide in (1, 2):   # title cards for the start of the video
            page.goto(f"{args.base}/?backend={args.base}&intro={slide}&speed={args.speed}")
            page.wait_for_selector(".intro", timeout=30_000)
            page.wait_for_timeout(800)
            page.screenshot(path=f"/out/intro-{slide}.png")
    page.goto(f"{args.base}/?backend={args.base}")
    page.wait_for_selector("#status.ok", timeout=30_000)
    page.wait_for_timeout(1500)
    if args.check:
        page.screenshot(path="/out/check.png")
    else:
        done = threading.Event()
        threading.Thread(target=run_acts, args=(done,), daemon=True).start()
        stamps = []
        while not done.is_set():
            t = time.monotonic()
            path = f"/out/frames/{len(stamps):06d}.jpg"
            page.screenshot(path=path, type="jpeg", quality=93)
            stamps.append((path, t))
        with open("/out/frames.txt", "w") as f:
            for (path, t), (_, t_next) in zip(stamps, stamps[1:] + [(None, t + 0.1)]):
                f.write(f"file 'frames/{os.path.basename(path)}'\nduration {t_next - t:.4f}\n")
        span = stamps[-1][1] - stamps[0][1]
        print(f"{len(stamps)} frames over {span:.0f} s ({len(stamps) / span:.1f} fps)")
    browser.close()
