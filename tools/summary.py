#!/usr/bin/env python3
"""Per-lane numbers for one run, computed from its trace.

    tools/demo.py trace | tools/summary.py
    tools/summary.py run.json
"""

import json
import sys


def gaps(frames, start, end, threshold=0.5):
    """Total and longest time without output between start and end."""
    total = longest = 0.0
    last = start
    for t in frames:
        if t <= start:
            continue
        if t > end:
            break
        if t - last > threshold:
            total += t - last
            longest = max(longest, t - last)
        last = t
    return total, longest


def main():
    trace = json.load(open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin)
    ev = trace["events"]
    acts = {e["act"]: e["t"] for e in ev if e["kind"] == "act" and e["state"] == "start"}
    lanes = trace["settings"]["lanes"]
    rows = []
    for lane in lanes:
        mine = [e for e in ev if e.get("lane") == lane]
        frames = [e["t"] for e in mine if e["kind"] == "frame"]
        phase = lambda p, **k: [e for e in mine if e["kind"] == "phase" and e["phase"] == p
                                and e["state"] == "end" and all(e.get(a) == b for a, b in k.items())]
        ready = {e["what"]: e["seconds"] for e in mine if e["kind"] == "ready"}
        build = lambda v: sum(e["seconds"] for p in ("build", "push") for e in phase(p, version=v))
        samples = [e for e in ev if e["kind"] == "sample"]

        def bytes_at(t):
            prior = [s["lanes"][lane]["bytes"] for s in samples if s["t"] <= t]
            return prior[-1] if prior else 0

        upd = acts.get("update")
        v2 = ready.get("v2 live")
        blind, longest = gaps(frames, upd, upd + v2) if upd is not None and v2 else (None, None)
        rollout = [e["t"] for e in mine if e["kind"] == "rollout" and e.get("version") == "v3"]
        v3 = ready.get("v3 live")
        p_blind = p_longest = p_mb = None
        if rollout and v3:
            p_blind, p_longest = gaps(frames, rollout[0], rollout[0] + v3)
            p_mb = round((bytes_at(rollout[0] + v3 + 1) - bytes_at(rollout[0])) / 1e6)
        steady = [t for t in frames if acts.get("enable", 0) - 10 < t < acts.get("enable", 0)]
        rows.append({
            "lane": lane,
            "build v1 (s)": round(build("v1"), 1), "build v2 (s)": round(build("v2"), 1),
            "first frame (s)": ready.get("first frame"),
            "MB by first frame": round(bytes_at(acts["deploy"] + (ready.get("first frame") or 0) + 1) / 1e6),
            "segmenter (s)": ready.get("segmenter output"),
            "v2 live (s)": v2, "blind (s)": blind and round(blind, 2),
            "longest gap (s)": longest and round(longest, 2),
            "v3 build (s)": round(build("v3"), 1), "v3 live (s)": v3,
            "v3 blind (s)": p_blind and round(p_blind, 2), "v3 MB": p_mb,
            "fps before act 3": round(len(steady) / 10, 1),
            "MB total": round(samples[-1]["lanes"][lane]["bytes"] / 1e6) if samples else None,
        })
    keys = list(rows[0])
    width = max(len(k) for k in keys)
    for k in keys:
        print(f"{k:<{width}}  " + "  ".join(f"{str(r[k]):>12}" for r in rows))


if __name__ == "__main__":
    main()
