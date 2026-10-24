#!/usr/bin/env python3
"""Per-lane numbers for one run, computed from its trace with the same
definitions as the dashboard (dashboard/app.js, measure()).

    tools/demo.py trace | tools/summary.py
    tools/summary.py run.json

time to change   from an act's start to the lane's first frame of the new
                 version (Act 1: to its first frame at all)
downtime         gaps over 1 s without output inside that window
total time       time to change summed over all acts
total build      build and push time summed over every version
"""

import json
import sys

UPDATES = [("add-model", "v2"), ("upgrade", "v3"), ("patch", "v4")]
BLIND = 1.0


def gaps(frames, start, end):
    total, last = 0.0, start
    for t in frames:
        if t <= start:
            continue
        if t > end + 0.05:
            break
        if t - last > BLIND:
            total += t - last
        last = t
    if end - last > BLIND:
        total += end - last
    return total


def main():
    trace = json.load(open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin)
    ev = trace["events"]
    acts = {e["act"]: e["t"] for e in ev if e["kind"] == "act" and e["state"] == "start"}
    samples = [e for e in ev if e["kind"] == "sample"]
    rows = []
    for lane in trace["settings"]["lanes"]:
        mine = [e for e in ev if e.get("lane") == lane]
        frames = [e["t"] for e in mine if e["kind"] == "frame"]
        live = {e["what"].split()[0]: e["t"] for e in mine if e["kind"] == "ready"
                and e["what"].endswith(" live")}
        row = {"lane": lane}
        first = frames[0] if frames else None
        deploy = first - acts["deploy"] if first is not None and "deploy" in acts else None
        row["Act 1 to first frame (s)"] = deploy and round(deploy, 1)
        total, down = deploy, 0.0
        for act, v in UPDATES:
            if act not in acts:
                continue
            start = acts[act]
            end = live.get(v)
            row[f"{v} live after (s)"] = end and round(end - start, 1)
            d = gaps(frames, start, end if end else frames[-1])
            row[f"{v} downtime (s)"] = round(d, 2)
            down += d
            total = total + (end - start) if total is not None and end else None
        row["total downtime (s)"] = round(down, 2)
        row["total time (s)"] = total and round(total, 1)
        for v in ("v1", "v2", "v3", "v4"):
            b = [e for e in mine if e["kind"] == "phase" and e["state"] == "end"
                 and e["phase"] in ("build", "push") and e.get("version") == v]
            if b:
                rebuilt = next((e for e in b if "of" in e), None)
                row[f"{v} build+push (s)"] = round(sum(e["seconds"] for e in b), 1)
                if rebuilt:
                    row[f"{v} rebuilt"] = f"{rebuilt['rebuilt']}/{rebuilt['of']}"
        builds = [row[f"{v} build+push (s)"] for v in ("v1", "v2", "v3", "v4")
                  if f"{v} build+push (s)" in row]
        row["total build+push (s)"] = round(sum(builds), 1) if builds else None
        row["MB total"] = round(samples[-1]["lanes"][lane]["bytes"] / 1e6) if samples else None
        rows.append(row)
    keys = list(dict.fromkeys(k for r in rows for k in r))
    width = max(len(k) for k in keys)
    for k in keys:
        print(f"{k:<{width}}  " + "  ".join(f"{str(r.get(k)):>12}" for r in rows))


if __name__ == "__main__":
    main()
