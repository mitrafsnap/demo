"""Scenario preparation: download assets once, then lay out each version as one
directory per slot, plus the build inputs for every method.

    work/<scenario>/<version>/<slot>/<file>     slot contents
    work/<scenario>/<version>/2dfs.json         one allotment per slot (row 0)
    work/<scenario>/<version>/Dockerfile        one COPY layer per slot

Unchanged slots are hard links to the same asset, so every builder sees
identical bytes for them across versions.
"""

import asyncio
import hashlib
import json
import os
import shutil
import urllib.request
import zipfile

ROOT = os.environ.get("DEMO_DATA", "/var/lib/demo")
ASSETS = os.path.join(ROOT, "assets")
WORK = os.path.join(ROOT, "work")
SCENARIOS = os.environ.get("DEMO_SCENARIOS", "/opt/demo/scenarios")


def load(name):
    with open(os.path.join(SCENARIOS, name, "scenario.json")) as f:
        return json.load(f)


def available():
    return sorted(d for d in os.listdir(SCENARIOS)
                  if os.path.isfile(os.path.join(SCENARIOS, d, "scenario.json")))


def asset_path(name, spec):
    """Assets are shared by scenarios, so the file name includes the URL: two
    scenarios may use the same key for different files."""
    source = spec.get("extract") or spec["url"].split("?")[0]
    tag = hashlib.sha256(spec["url"].encode()).hexdigest()[:8]
    return os.path.join(ASSETS, f"{name}-{tag}{os.path.splitext(source)[1]}")


def sha256(path):
    side = path + ".sha256"
    if os.path.exists(side) and os.path.getmtime(side) >= os.path.getmtime(path):
        return open(side).read().strip()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    open(side, "w").write(h.hexdigest())
    return h.hexdigest()


def _download(url, dest, progress):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "mitrafsnap-demo/0.1"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            progress(done, total)
    os.replace(tmp, dest)


def _extract(archive, member, dest):
    """Copy the archive member whose name ends with member to dest."""
    with zipfile.ZipFile(archive) as z:
        name = next(n for n in z.namelist() if n.endswith("/" + member) or n == member)
        with z.open(name) as src, open(dest + ".part", "wb") as out:
            shutil.copyfileobj(src, out)
    os.replace(dest + ".part", dest)


async def fetch_assets(scn, emit):
    os.makedirs(ASSETS, exist_ok=True)
    for name, spec in scn["assets"].items():
        dest = asset_path(name, spec)
        if os.path.exists(dest):
            continue
        emit("prepare", step="download", asset=name, done=0, total=0)
        loop = asyncio.get_running_loop()
        last = [0]

        def progress(done, total, name=name):
            if done - last[0] >= 16 << 20 or done == total:
                last[0] = done
                loop.call_soon_threadsafe(lambda: emit(
                    "prepare", step="download", asset=name, done=done, total=total))

        if spec.get("extract"):
            archive = dest + ".download"
            await asyncio.to_thread(_download, spec["url"], archive, progress)
            await asyncio.to_thread(_extract, archive, spec["extract"], dest)
            os.remove(archive)
        else:
            await asyncio.to_thread(_download, spec["url"], dest, progress)


def version_specs(scn, version):
    """v1 is complete; later versions override individual slots."""
    specs = {k: dict(v) for k, v in scn["versions"]["v1"].items()}
    names = list(scn["versions"])
    for v in names[1:names.index(version) + 1]:
        for slot, override in scn["versions"][v].items():
            specs[slot] = dict(override)
    return specs


def materialize(scn, version):
    """Write work/<scenario>/<version>/ and return its path."""
    base = os.path.join(WORK, scn["name"], version)
    if os.path.exists(base):
        shutil.rmtree(base)
    models = {}
    for slot in scn["slots"]:
        os.makedirs(os.path.join(base, slot))
        if slot == "config":
            continue
        spec = version_specs(scn, version)[slot]
        dest = os.path.join(base, slot, spec["file"])
        if spec.get("placeholder"):
            # A slot reserved for a model a later version ships, so the image
            # keeps the same layout across versions.
            with open(dest, "w") as f:
                f.write(f"{slot}: not shipped in {version}\n")
            src = dest
        else:
            src = asset_path(spec["asset"], scn["assets"][spec["asset"]])
            os.link(src, dest)
        models[slot] = {k: v for k, v in spec.items() if k not in ("asset", "placeholder")}
        models[slot]["sha256"] = sha256(src)
        models[slot]["bytes"] = os.path.getsize(src)
    config = {"scenario": scn["name"], "version": version, "models": models}
    with open(os.path.join(base, "config", "config.json"), "w") as f:
        json.dump(config, f, indent=2, sort_keys=True)

    allotments = []
    for col, slot in enumerate(scn["slots"]):
        files = sorted(os.listdir(os.path.join(base, slot)))
        allotments.append({"src": [f"{slot}/{f}" for f in files],
                           "dst": [f"/models/{f}" for f in files], "row": 0, "col": col})
    with open(os.path.join(base, "2dfs.json"), "w") as f:
        json.dump({"allotments": allotments}, f, indent=2)

    lines = ["ARG BASE", "FROM ${BASE}"]
    lines += [f"COPY {slot}/ /models/" for slot in scn["docker_order"]]
    with open(os.path.join(base, "Dockerfile"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return base


def partition(scn, slots):
    """2DFS tag suffix selecting the given slots (row 0, one column each)."""
    return "".join(f"--0.{scn['slots'].index(s)}.0.{scn['slots'].index(s)}" for s in slots)


def slot_sizes(scn, version):
    base = os.path.join(WORK, scn["name"], version)
    out = {}
    for slot in scn["slots"]:
        d = os.path.join(base, slot)
        out[slot] = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
    return out
