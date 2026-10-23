"""Screenshot the dashboard at intervals with headless Chromium (Playwright).

    docker run --rm -v "$PWD/tools:/tools" -v "$OUT:/out" \
      mcr.microsoft.com/playwright/python:v1.55.0-noble \
      python3 /tools/screenshot.py http://host.docker.internal:8080 5 10 20
"""

import sys
import time

from playwright.sync_api import sync_playwright

base, times = sys.argv[1], [float(t) for t in sys.argv[2:]] or [5]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
    page.on("console", lambda m: print("console:", m.text))
    page.on("pageerror", lambda e: print("pageerror:", e))
    page.goto(f"{base}/?backend={base}")
    t0 = time.time()
    for t in times:
        time.sleep(max(0, t - (time.time() - t0)))
        page.screenshot(path=f"/out/shot-{int(t):04d}.png", full_page=True)
        print("shot", t)
    browser.close()
