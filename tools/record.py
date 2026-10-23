"""Record the dashboard while the browser clicks through every act.

    docker run --rm -v "$PWD/tools:/tools" -v "$OUT:/out" \
      mcr.microsoft.com/playwright/python:v1.55.0-noble \
      python3 /tools/record.py http://host.docker.internal:8080 [--lanes all] [--check]

Writes /out/dashboard.webm (or /out/check.png with --check).
"""

import argparse
import glob
import os
import time

from playwright.sync_api import sync_playwright

ACTS = ["prepare", "reset", "build-v1", "deploy", "enable", "build-v2", "update", "patch"]
W, H = 1920, 1080

ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("--lanes", default="all")
ap.add_argument("--zoom", default="0.8")
ap.add_argument("--check", action="store_true")
args = ap.parse_args()

IDLE = """() => [...document.querySelectorAll('#acts button')].every(b => !b.disabled)"""


def act_done(act):
    if act == "reset":
        return IDLE
    return f"""() => document.querySelector('[data-act="{act}"]')?.classList.contains('done')
                  && [...document.querySelectorAll('#acts button')].every(b => !b.disabled)"""


with sync_playwright() as p:
    browser = p.chromium.launch()
    opts = {"viewport": {"width": W, "height": H}}
    if not args.check:
        opts |= {"record_video_dir": "/out/raw", "record_video_size": {"width": W, "height": H}}
    ctx = browser.new_context(**opts)
    page = ctx.new_page()
    page.on("pageerror", lambda e: print("pageerror:", e))
    page.goto(f"{args.base}/?backend={args.base}")
    page.wait_for_selector("#status.ok", timeout=30_000)
    page.evaluate(f"document.body.style.zoom = '{args.zoom}'")
    if args.lanes == "all":
        page.click("#all-lanes")
    page.wait_for_timeout(1500)
    if args.check:
        page.screenshot(path="/out/check.png")
    else:
        for act in ACTS:
            page.wait_for_function(IDLE, timeout=60_000)
            page.click(f'[data-act="{act}"]')
            page.wait_for_timeout(1000)
            page.wait_for_function(act_done(act), timeout=900_000, polling=500)
            print("done", act, flush=True)
            page.wait_for_timeout(4000)
        page.wait_for_timeout(6000)
    ctx.close()
    browser.close()
    if not args.check:
        raw = max(glob.glob("/out/raw/*.webm"), key=os.path.getmtime)
        os.replace(raw, "/out/dashboard.webm")
