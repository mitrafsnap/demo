#!/usr/bin/env python3
"""Drive the demo backend from a terminal.

    tools/demo.py info
    tools/demo.py lanes docker mitrafsnap
    tools/demo.py act prepare build-v1 deploy ...   run acts in order, print events
    tools/demo.py trace > run.json

Set DEMO_BACKEND (default http://localhost:8080) and DEMO_TOKEN if needed.
"""

import json
import os
import sys
import urllib.request

BASE = os.environ.get("DEMO_BACKEND", "http://localhost:8080").rstrip("/")
TOKEN = os.environ.get("DEMO_TOKEN", "")


def call(path, body=None):
    req = urllib.request.Request(BASE + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    return urllib.request.urlopen(req)


def show(ev):
    k = ev["kind"]
    lane = f"{ev['lane']:>10} " if ev.get("lane") else " " * 11
    if k == "phase" and ev["state"] != "start":
        extra = f" {ev.get('seconds', '')}s" + (f" {ev['bytes'] / 1e6:.1f}MB" if ev.get("bytes") else "")
        print(f"{ev['t']:8.2f} {lane}{ev['phase']} {ev['state']}{extra} {ev.get('error', '')}")
    elif k == "phase":
        print(f"{ev['t']:8.2f} {lane}{ev['phase']} start")
    elif k == "app":
        rest = {x: y for x, y in ev.items() if x not in ("t", "kind", "lane", "phase")}
        print(f"{ev['t']:8.2f} {lane}app {ev['phase']} {rest if rest else ''}")
    elif k in ("act", "prepare", "lane_error", "redeploy", "log", "refresh_layer"):
        rest = {x: y for x, y in ev.items() if x not in ("t", "kind", "lane", "sizes")}
        print(f"{ev['t']:8.2f} {lane}{k} {rest}")


def run_act(name):
    stream = call("/api/events")
    call(f"/api/acts/{name}", {})
    first = {}
    for raw in stream:
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        if ev["kind"] == "frame":
            if ev["lane"] not in first or first[ev["lane"]] != ev.get("version"):
                first[ev["lane"]] = ev.get("version")
                print(f"{ev['t']:8.2f} {ev['lane']:>10} frame {ev.get('version')} "
                      f"models={ev.get('models')} objects={ev.get('objects')}")
            continue
        show(ev)
        if ev["kind"] == "act" and ev["act"] == name and ev["state"] in ("end", "error"):
            return ev["state"] == "end"


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "info":
        print(json.dumps(json.load(call("/api/info")), indent=2))
    elif cmd == "lanes":
        print(json.load(call("/api/settings", {"lanes": args})))
    elif cmd == "set":
        print(json.load(call("/api/settings", {k: json.loads(v) for k, v in
                                               (a.split("=", 1) for a in args)})))
    elif cmd == "act":
        for name in args:
            if not run_act(name):
                sys.exit(1)
    elif cmd == "trace":
        print(json.dumps(json.load(call("/api/trace"))))


if __name__ == "__main__":
    main()
