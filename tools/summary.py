#!/usr/bin/env python3
"""Per-lane numbers for one run, computed from its trace.

    tools/demo.py trace | tools/summary.py
    tools/summary.py run.json
"""

import json
import sys


def gaps(frames, start, end, threshold=1.0):
    """Total and longest time without output between start and end. Gaps under
    threshold are slow frames (a loaded CPU), not an outage."""
    total = longest = 0.0
    last = start
    for t in frames:
        if t <= start:
            continue
        if t > end + 0.05:   # the first new-version frame closes the window
            break
        if t - last > threshold:
            total += t - last
            longest = max(longest, t - last)
        last = t
    return total, longest


def main():
    trace = json.load(open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin)
    ev = trace["events"]
    samples = [e for e in ev if e["kind"] == "sample"]
    acts = {e["act"]: e["t"] for e in ev if e["kind"] == "act" and e["state"] == "start"}
    rows = []
    for lane in trace["settings"]["lanes"]:
        mine = [e for e in ev if e.get("lane") == lane]
        frames = [e["t"] for e in mine if e["kind"] == "frame"]
        ready = {e["what"]: e["seconds"] for e in mine if e["kind"] == "ready"}

        def bytes_at(t):
            prior = [s["lanes"][lane]["bytes"] for s in samples if s["t"] <= t]
            return prior[-1] if prior else 0

        def build(v):
            return round(sum(e["seconds"] for e in mine if e["kind"] == "phase"
                             and e["phase"] in ("build", "push") and e["state"] == "end"
                             and e.get("version") == v), 1)

        row = {"lane": lane}
        for v in ("v1", "v2", "v3", "v4"):
            row[f"build {v} (s)"] = build(v)
        first = ready.get("first frame")
        row["first frame (s)"] = first
        row["MB by first frame"] = round(bytes_at(acts["deploy"] + (first or 0) + 1) / 1e6) \
            if "deploy" in acts else None
        for e in mine:
            if e["kind"] != "rollout":
                continue
            v, start = e["version"], e["t"]
            live = ready.get(f"{v} live")
            blind = gaps(frames, start, start + live)[0] if live else None
            row[f"{v} live (s)"] = live
            row[f"{v} blind (s)"] = blind if blind is None else round(blind, 2)
            row[f"{v} MB"] = round((bytes_at(start + (live or 0) + 1) - bytes_at(start)) / 1e6) \
                if live else None
        row["MB total"] = round(samples[-1]["lanes"][lane]["bytes"] / 1e6) if samples else None
        rows.append(row)
    keys = list(dict.fromkeys(k for r in rows for k in r))
    width = max(len(k) for k in keys)
    for k in keys:
        print(f"{k:<{width}}  " + "  ".join(f"{str(r.get(k)):>12}" for r in rows))


if __name__ == "__main__":
    main()
