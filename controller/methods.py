"""The four methods. Each one builds, pushes, deploys and updates the same
scenario its own way, and reports every phase on the bus.

Each method runs in its own containerd namespace with content sharing turned
off (see node/etc/containerd/config.toml), so no lane can reuse another's
downloads. Lanes pull through their own link port (see link.py).
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import time
import urllib.error
import urllib.request

import aiohttp

import scenario

LOCAL_REGISTRY = os.environ.get("DEMO_REGISTRY", "registry:5000")
REGISTRY = LOCAL_REGISTRY      # set_registry() changes it to a registry on another machine
CONTROLLER = "http://127.0.0.1:8080"
TDFS_HOME = os.path.join(scenario.ROOT, "tdfs")


async def sh(*cmd, check=True):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    out = out.decode(errors="replace")
    if check and proc.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}):\n{out[-2000:]}")
    return out


LANE_DIR = os.environ.get("DEMO_LANE", "/opt/demo/lane")


def lane_hash():
    """Short digest of the lane app's source, so a code change gets a new base."""
    h = hashlib.sha256(b"oci-base")   # bump when prepare_base changes
    for name in sorted(os.listdir(LANE_DIR)):
        path = os.path.join(LANE_DIR, name)
        if os.path.isfile(path):
            h.update(name.encode())
            h.update(open(path, "rb").read())
    return h.hexdigest()[:10]


# Pinned by prepare, so editing the lane source mid-run can't change which base
# later builds use. Includes the device (cpu or gpu) the runtime was built for.
BASE_TAG = None


ACCEL_PORT = int(os.environ.get("DEMO_ACCEL_PORT", "8090"))


def cuda_available():
    """An NVIDIA GPU passed into the node (compose.gpu.yaml) that ctr run --gpus
    can hand to lane containers through nvidia-container-cli."""
    return os.path.exists("/dev/nvidia0") and shutil.which("nvidia-container-cli") is not None


# Set by prepare when the Apple GPU helper is used, so every lane container of
# the run gets the same address without probing the helper again.
APPLE_URL = None


def apple_url():
    """URL of the Apple GPU helper (tools/accel) on the Docker Desktop host, as
    an IP address: lane containers can't resolve host.docker.internal."""
    try:
        ip = socket.gethostbyname("host.docker.internal")
        with urllib.request.urlopen(f"http://{ip}:{ACCEL_PORT}/health", timeout=5):
            return f"http://{ip}:{ACCEL_PORT}"
    except OSError:
        return None


def accelerators():
    return {"cuda": cuda_available(), "apple": apple_url() is not None}


HOW_TO = {"cuda": "start the backend with compose.gpu.yaml on a machine with an NVIDIA GPU",
          "apple": "run tools/accel/run.sh on the Mac"}


def device(setting):
    """Effective device (cuda, apple or cpu) for a setting of auto, cuda, apple or
    cpu. auto picks the first accelerator found, else the CPU."""
    if setting == "cpu":
        return "cpu"
    found = accelerators()
    if setting in found:
        if not found[setting]:
            raise RuntimeError(f"no {setting} device: {HOW_TO[setting]}")
        return setting
    return next((d for d in ("cuda", "apple") if found[d]), "cpu")


def base_tag(dev):
    """The lane runtime differs only for CUDA; the Apple helper runs outside it."""
    return f"{'cuda' if dev == 'cuda' else 'cpu'}-{lane_hash()}"


def base_ref(variant):
    return f"{REGISTRY}/base/lane-runtime:{variant}-{BASE_TAG or base_tag('cpu')}"


class Method:
    key = title = ""
    base = "plain"         # base image variant
    lazy = False           # uses the stargz snapshotter
    partial = False        # pulls only the selected slots
    refresh = False        # updates in place

    def __init__(self, index, bus, link, settings):
        self.index, self.bus, self.link, self.settings = index, bus, link, settings
        self.port = 8101 + index           # lane app control port
        self.container = f"lane-{self.key}"
        self.deployed = None               # pulled reference
        self.selected = []                 # slots selected at deploy
        self.enabled = []                  # models enabled in the app
        self.watch = None
        self.relay = None                  # set by the builder API to stream phases back

    # -- helpers -----------------------------------------------------------

    @property
    def scn(self):
        return self.settings["scn"]

    def repo(self, host=None):
        return f"{host or REGISTRY}/{self.key}/{self.scn['name']}"

    def emit(self, kind, **data):
        if self.relay:
            self.relay(kind, data)
        else:
            self.bus.emit(kind, self.key, **data)

    async def phase(self, name, coro, report=None, **data):
        """Run coro as one named phase and report its start, end and link bytes.
        report(result) can add fields to the end event."""
        b0 = self.link.bytes(self.key)
        self.emit("phase", phase=name, state="start", **data)
        t0 = time.monotonic()
        try:
            result = await coro
        except Exception as e:
            self.emit("phase", phase=name, state="error", error=str(e)[-500:])
            raise
        extra = report(result) if report else {}
        self.emit("phase", phase=name, state="end", seconds=round(time.monotonic() - t0, 3),
                  bytes=self.link.bytes(self.key) - b0, **data, **extra)
        return result

    def ctr(self, *args):
        tool = "ctr-remote" if self.lazy else "ctr"
        return (tool, "-n", self.key, *args)

    # -- build -------------------------------------------------------------

    async def build(self, version):
        """Build and push version, here or on the builder machine."""
        if self.settings.get("builder"):
            await self.on_builder("build", version=version)
        else:
            await self.build_here(version)

    async def clean_build_cache(self):
        if self.settings.get("builder"):
            await self.on_builder("clean")
        else:
            await self.clean_here()

    async def on_builder(self, action, **body):
        """Run a build action on the builder machine (another demo backend) and
        replay the phases it streams back as this lane's events."""
        url = f"{self.settings['builder'].rstrip('/')}/api/builder/{action}"
        token = self.settings.get("builder_token")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body |= {"method": self.key, "scenario": self.scn["name"], "registry": REGISTRY,
                 "base_tag": BASE_TAG}
        timeout = aiohttp.ClientTimeout(total=1800, sock_read=900)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"builder {url}: HTTP {resp.status} {await resp.text()}")
                async for line in resp.content:
                    ev = json.loads(line)
                    if ev["kind"] == "failed":
                        raise RuntimeError(f"builder: {ev['error']}")
                    if ev["kind"] != "done":
                        self.emit(ev.pop("kind"), **ev)

    async def build_here(self, version):
        raise NotImplementedError

    async def clean_here(self):
        pass

    async def buildkit(self, version, extra_output=""):
        ctx = os.path.join(scenario.WORK, self.scn["name"], version)
        ref = f"{self.repo()}:live"
        out = f"type=image,name={ref},push=false{extra_output}"
        await self.phase("build", sh(
            "buildctl", "build", "--frontend", "dockerfile.v0",
            "--local", f"context={ctx}", "--local", f"dockerfile={ctx}",
            "--opt", f"build-arg:BASE={base_ref(self.base)}", "--output", out,
            "--progress", "plain"), report=buildkit_rebuilt, version=version, tool="BuildKit")
        await self.phase("push", sh("ctr", "-n", "buildkit", "images", "push", "--plain-http",
                                    ref), version=version)

    async def tdfs(self, version, *flags):
        ctx = os.path.join(scenario.WORK, self.scn["name"], version)
        ref = f"{self.repo()}:live"
        home = os.path.join(TDFS_HOME, self.key)
        os.makedirs(home, exist_ok=True)
        cmd = ["tdfs", "--home-dir", home, "build", "--force-http", *flags, "-f", "2dfs.json",
               base_ref(self.base), ref]

        async def run():
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=ctx, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            if proc.returncode:
                raise RuntimeError(out.decode()[-2000:])
            return out.decode(errors="replace")

        await self.phase("build", run(), report=self.tdfs_rebuilt, version=version, tool="tdfs")
        await self.phase("push", sh("tdfs", "--home-dir", home, "image", "push", "--force-http",
                                    ref), version=version)

    def tdfs_rebuilt(self, log):
        """tdfs logs [CREATED] for each allotment it builds; cached ones it reuses."""
        created = len(re.findall(r"Allotment \d+/\d+ \S+ \[CREATED\]|Alltoment \d+/\d+ \S+ \[CREATED\]", log))
        return {"rebuilt": created, "of": len(self.scn["slots"])}

    # -- deploy ------------------------------------------------------------

    def pull_ref(self, slots):
        suffix = scenario.partition(self.scn, slots) if self.partial or self.lazy else ""
        return f"{self.repo(self.link.host(self.key))}:live{suffix}"

    async def pull(self, ref):
        if self.lazy:
            await sh(*self.ctr("images", "rpull", "--plain-http", ref))
        else:
            await sh(*self.ctr("images", "pull", "--plain-http", ref))

    async def start(self, ref):
        cpus = self.settings.get("cpus")
        env = {"LANE": self.key, "PORT": str(self.port), "CONTROLLER": CONTROLLER,
               "THREADS": str(self.settings.get("threads", 2)),
               "ENABLED": ",".join(self.enabled)}
        env["DEVICE"] = self.settings.get("effective_device", "cpu")
        args = ["run", "-d", "--net-host", f"--log-uri=file:///var/log/node/{self.container}.log"]
        if env["DEVICE"] == "cuda":
            args += ["--gpus", "0"]
        if env["DEVICE"] == "apple":
            env["ACCEL_URL"] = APPLE_URL or ""
        if self.lazy:
            args.append("--snapshotter=stargz")
        if cpus:
            args.append(f"--cpus={cpus}")
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
        await sh(*self.ctr(*args, ref, self.container))

    async def deploy(self, slots, enabled):
        self.selected, self.enabled = list(slots), list(enabled)
        ref = self.pull_ref(slots)
        await self.phase("pull", self.pull(ref), ref=ref,
                         slots=slots if (self.partial or self.lazy) else "all")
        await self.phase("start", self.start(ref))
        self.deployed = ref

    async def running(self):
        """Whether this lane's container is still running, and its last log lines."""
        out = await sh(*self.ctr("tasks", "ls"), check=False)
        alive = any(line.split()[:1] == [self.container] and "RUNNING" in line
                    for line in out.splitlines())
        log = ""
        if not alive:
            log = await sh("tail", "-n", "8", f"/var/log/node/{self.container}.log", check=False)
        return alive, log

    async def stop(self):
        await self.stop_watch()
        if not self.deployed:
            return

        async def kill():
            await sh(*self.ctr("tasks", "kill", "-s", "KILL", self.container), check=False)
            for _ in range(50):
                out = await sh(*self.ctr("tasks", "ls"), check=False)
                if self.container not in out or "STOPPED" in out:
                    break
                await asyncio.sleep(0.1)
            await sh(*self.ctr("tasks", "rm", "-f", self.container), check=False)
            await sh(*self.ctr("containers", "rm", self.container), check=False)

        await self.phase("stop", kill())
        self.deployed = None

    # -- update -------------------------------------------------------

    async def update(self):
        """Replacement: stop, pull the new version, start, let the app reload."""
        slots = sorted(set(self.selected) | set(self.enabled))
        await self.stop()
        await self.deploy(slots, self.enabled)

    async def stop_watch(self):
        pass

    # -- reset -------------------------------------------------------------

    async def clean(self):
        """Remove everything this method left in its namespace, whether or not
        this controller process deployed it."""
        await self.stop_watch()
        await sh(*self.ctr("tasks", "kill", "-s", "KILL", self.container), check=False)
        await asyncio.sleep(0.3)
        await sh(*self.ctr("tasks", "rm", "-f", self.container), check=False)
        await sh(*self.ctr("containers", "rm", self.container), check=False)
        snapshotter = "stargz" if self.lazy else "overlayfs"
        await sh("ctr", "-n", self.key, "snapshots", "--snapshotter", snapshotter, "rm",
                 self.container, check=False)
        self.deployed = None
        out = await sh(*self.ctr("images", "ls", "-q"), check=False)
        refs = [r for r in out.split() if r]
        if refs:
            await sh(*self.ctr("images", "rm", *refs), check=False)
        await sh("ctr", "-n", self.key, "content", "prune", "references", check=False)
        self.selected, self.enabled = [], []

class Docker(Method):
    key, title = "docker", "Docker"

    async def build_here(self, version):
        await self.buildkit(version)

    async def clean_here(self):
        await sh("buildctl", "prune", "--all", check=False)
        await sh("ctr", "-n", "buildkit", "images", "rm", f"{self.repo()}:live", check=False)


class EStargz(Method):
    key, title = "estargz", "eStargz"
    base, lazy = "esgz", True

    async def build_here(self, version):
        await self.buildkit(version, ",compression=estargz,force-compression=true,"
                                     "oci-mediatypes=true")

    clean_here = Docker.clean_here


class TwoDFS(Method):
    key, title = "twodfs", "2DFS"
    partial = True

    async def build_here(self, version):
        await self.tdfs(version)

    async def clean_here(self):
        shutil.rmtree(os.path.join(TDFS_HOME, self.key), ignore_errors=True)


class MitraFSnap(Method):
    key, title = "mitrafsnap", "MitraFSnap"
    base, lazy, partial, refresh = "zstd", True, True, True

    async def build_here(self, version):
        await self.tdfs(version, "--enable-stargz", "--use-zstd")

    clean_here = TwoDFS.clean_here

    async def deploy(self, slots, enabled):
        await super().deploy(slots, enabled)
        if self.settings.get("auto_watch"):
            await self.start_watch()

    async def start_watch(self):
        """Subscribe the snapshotter to the deployed tag. The daemon polls the
        registry and refreshes the mount itself; we follow its log."""
        interval = self.settings.get("watch_interval", "1s")
        await sh("ctr-remote", "-n", self.key, "watch", "--interval", interval, self.deployed)
        self.emit("watch", state="start", ref=self.deployed, interval=interval)
        self.watch = asyncio.create_task(self.follow_log(self.deployed))

    async def stop_watch(self):
        if self.watch:
            self.watch.cancel()
            self.watch = None

    async def follow_log(self, ref):
        path = "/var/log/node/stargz.log"
        with open(path) as f:
            f.seek(0, os.SEEK_END)
            pending, t0, b0 = 0, 0.0, 0
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("watch_ref") != ref:
                    continue
                msg = rec.get("msg", "")
                if msg == "manifest changed, dispatching refresh":
                    pending = rec.get("layers_to_refresh", 1)
                    t0, b0 = time.monotonic(), self.link.bytes(self.key)
                    self.emit("phase", phase="refresh", state="start", layers=pending,
                              unchanged=rec.get("layers_unchanged"))
                elif msg == "layer blob refreshed successfully" and pending:
                    pending -= 1
                    self.emit("refresh_layer", **{k: v for k, v in rec.items()
                                                  if k not in ("msg", "level", "time")})
                    if not pending:
                        self.emit("phase", phase="refresh", state="end",
                                  seconds=round(time.monotonic() - t0, 3),
                                  bytes=self.link.bytes(self.key) - b0)
                elif rec.get("level") in ("warning", "error"):
                    self.emit("log", source="snapshotter", text=msg)

    async def update(self):
        """Refresh the running container's mount to the tag's new version. The
        app notices the new config.json and reloads what changed. With
        auto_watch, the snapshotter's subscription does this by itself."""
        if self.watch:
            return
        out = await self.phase("refresh", sh(
            *self.ctr("refresh", "--plain-http", self.deployed)))
        for line in out.splitlines():
            self.emit("log", source="refresh", text=line.strip())


METHODS = [Docker, EStargz, TwoDFS, MitraFSnap]


def buildkit_rebuilt(log):
    """COPY steps BuildKit ran (not CACHED), from its plain progress output. Each
    COPY is one model layer; a change invalidates every layer after it."""
    copies = set(re.findall(r"^#(\d+) \[\d+/\d+\] COPY ", log, re.M))
    cached = set(re.findall(r"^#(\d+) CACHED", log, re.M))
    return {"rebuilt": len(copies - cached), "of": len(copies)}


def warm_apple(scn):
    """Load every model a version of the scenario turns on into the Apple GPU
    helper, which compiles and caches it by digest."""
    url = APPLE_URL
    if not url:
        return
    paths = set()
    for v in scn["versions"]:
        for spec in scenario.version_specs(scn, v).values():
            if spec.get("placeholder") or not spec.get("enabled") or spec["kind"] == "yunet":
                continue   # YuNet runs in OpenCV, not ONNX Runtime
            for key in ("asset", "decoder_asset"):
                if spec.get(key):
                    paths.add(scenario.asset_path(spec[key], scn["assets"][spec[key]]))
    for path in sorted(paths):
        with open(path, "rb") as f:
            req = urllib.request.Request(f"{url}/models", data=f.read(), method="POST")
        urllib.request.urlopen(req, timeout=600).close()


def registry_local():
    return REGISTRY == LOCAL_REGISTRY


async def set_registry(host):
    """Point builders and lanes at a registry (host:port), or back at the local
    one for "". BuildKit must be told the registry speaks plain HTTP, so its
    config is rewritten and it restarts (node-entrypoint keeps it running)."""
    global REGISTRY
    REGISTRY = host or LOCAL_REGISTRY
    hosts = sorted({LOCAL_REGISTRY, REGISTRY})
    conf = "\n".join(['[worker.oci]', '  enabled = false', '', '[worker.containerd]',
                      '  enabled = true', '  snapshotter = "overlayfs"', ''] +
                     [f'[registry."{h}"]\n  http = true\n  insecure = true\n' for h in hosts])
    with open("/etc/buildkit/buildkitd.toml", "w") as f:
        f.write(conf)
    await start_buildkitd()


BUILDKITD = None


async def start_buildkitd():
    """(Re)start buildkitd with the current config and wait until it answers."""
    global BUILDKITD
    if BUILDKITD and BUILDKITD.returncode is None:
        BUILDKITD.terminate()
        await BUILDKITD.wait()
    log = open("/var/log/node/buildkitd.log", "ab")
    BUILDKITD = await asyncio.create_subprocess_exec(
        "buildkitd", "--config", "/etc/buildkit/buildkitd.toml", stdout=log, stderr=log)
    for _ in range(150):
        await asyncio.sleep(0.2)
        if "ID" in await sh("buildctl", "debug", "workers", check=False):
            return
    raise RuntimeError("buildkitd did not start; see /var/log/node/buildkitd.log")


def node_platform():
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(os.uname().machine, os.uname().machine)
    return {"architecture": arch, "os": "linux"}


def wrap_in_index(ref):
    """Re-tag a single-manifest image as an OCI index that names its platform.
    tdfs labels images without platform information as amd64."""
    host, rest = ref.split("/", 1)
    repo, tag = rest.rsplit(":", 1)
    accept = ", ".join(["application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json"])
    req = urllib.request.Request(f"http://{host}/v2/{repo}/manifests/{tag}",
                                 headers={"Accept": accept})
    with urllib.request.urlopen(req) as r:
        body, media = r.read(), r.headers["Content-Type"]
        digest = r.headers["Docker-Content-Digest"]
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
             "manifests": [{"mediaType": media, "digest": digest, "size": len(body),
                            "platform": node_platform()}]}
    req = urllib.request.Request(
        f"http://{host}/v2/{repo}/manifests/{tag}", data=json.dumps(index).encode(), method="PUT",
        headers={"Content-Type": "application/vnd.oci.image.index.v1+json"})
    urllib.request.urlopen(req).close()


def base_ready():
    """True when all three base images are published as platform indexes."""
    for variant in ("plain", "esgz", "zstd"):
        host, rest = base_ref(variant).split("/", 1)
        repo, tag = rest.rsplit(":", 1)
        req = urllib.request.Request(f"http://{host}/v2/{repo}/manifests/{tag}", headers={
            "Accept": "application/vnd.oci.image.index.v1+json"})
        try:
            with urllib.request.urlopen(req) as r:
                if "index" not in r.headers["Content-Type"]:
                    return False
        except urllib.error.URLError:
            return False
    return True


async def prepare_base(lane_dir, emit, dev):
    """Build the lane runtime once and publish it in the three base formats."""
    plain, esgz, zstd = base_ref("plain"), base_ref("esgz"), base_ref("zstd")
    flavor = "cuda" if dev == "cuda" else "cpu"
    emit("prepare", step=f"runtime image ({flavor})")
    await sh("buildctl", "build", "--frontend", "dockerfile.v0",
             "--local", f"context={lane_dir}", "--local", f"dockerfile={lane_dir}",
             "--opt", f"build-arg:DEVICE={flavor}",
             # The registry expands 2DFS fields only on OCI manifests.
             "--output", f"type=image,name={plain},push=true,registry.insecure=true,"
                         "oci-mediatypes=true")
    await sh("nerdctl", "pull", "-q", "--insecure-registry", plain)
    for ref, flag in ((esgz, "--estargz"), (zstd, "--zstdchunked")):
        emit("prepare", step=f"convert {ref.rsplit(':', 1)[1].split('-')[0]}")
        await sh("nerdctl", "image", "convert", flag, "--oci", plain, ref)
        await sh("nerdctl", "push", "-q", "--insecure-registry", ref)
    for ref in (plain, esgz, zstd):
        await asyncio.to_thread(wrap_in_index, ref)


async def reset_snapshotter():
    """Drop every lazily fetched byte so the next run starts cold."""
    await sh("sh", "-c", "pkill -f containerd-stargz-grpc; sleep 1; "
                         "grep -o ' /var/lib/containerd-stargz-grpc/[^ ]*' /proc/mounts | "
                         "sort -r | xargs -r -n1 umount -l; "
                         "rm -rf /var/lib/containerd-stargz-grpc/* "
                         "/run/containerd-stargz-grpc/containerd-stargz-grpc.sock", check=False)
    # node-entrypoint restarts the snapshotter; wait for its socket.
    for _ in range(100):
        if os.path.exists("/run/containerd-stargz-grpc/containerd-stargz-grpc.sock"):
            out = await sh("ctr", "plugins", "ls", check=False)
            if "stargz" in out:
                return
        await asyncio.sleep(0.2)
