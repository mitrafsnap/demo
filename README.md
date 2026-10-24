# MitraFSnap live demo

A vision application deployed and updated four ways at once: Docker, eStargz, 2DFS and
[MitraFSnap](https://github.com/mitrafsnap). Every method builds the same image, deploys it over
the same emulated edge link and rolls out the same updates while a camera stream keeps running.
The dashboard shows each method's video, where it is in the workflow, and live charts of what
each one costs.

![Rolling out an update: replacement lanes go dark, MitraFSnap keeps serving](docs/traffic-highlight.gif)

Above: Act 3 at real speed. Docker, eStargz and 2DFS stop their containers to roll out a new
detector; MitraFSnap updates the running container and never stops serving.

Full runs, all four acts with all four methods. Each opens with what the acts are and how fast
it plays:

- ▶ **[Traffic camera (MP4)](docs/traffic.mp4)** · [GIF excerpt](docs/traffic-highlight.gif)
- ▶ **[People camera (MP4)](docs/people.mp4)** · [GIF excerpt](docs/people-highlight.gif)

The videos are 1280 px wide and play at 2× speed. `tools/encode.sh` also writes full-resolution
versions, which are not in the repository.

## What the demo shows

Two scenarios, picked in the dashboard's **Scenario** menu, run the same four acts:

- **traffic**: a city camera at a busy crossing (recorded clip). v1 detects people and cars;
  v2 adds instance segmentation (MobileSAM); v3 ships a stronger detector that also recognizes
  bicycles, motorcycles, buses and trucks; v4 patches a 528 MB model the camera never loads.
- **people**: a camera that watches people, fed by **your webcam** (or a recorded clip until
  you turn it on). v1 finds faces; v2 adds body pose; v3 ships an expression model that labels
  smiles; v4 patches the same unused model.

| Act | What happens | What MitraFSnap does differently |
|---|---|---|
| 1 Deploy v1 | every method builds v1, pushes it and starts the camera | starts before the image is downloaded and fetches only what the app reads |
| 2 Add a model | v2 adds a model; every method rebuilds, pushes and rolls out | rebuilds one part, then updates the running container instead of restarting it |
| 3 Upgrade a model | v3 replaces the detector (or adds the expression model) | same: Docker and eStargz rebuild every layer after the changed one and restart |
| 4 Patch unused model | v4 replaces a 528 MB model the camera never loads | refreshes in milliseconds and downloads only the new file index |

Docker rebuilds on every update, and BuildKit's cache keeps the layers before the changed one.
The Dockerfile adds one layer per model in the order the models were introduced, so a change
invalidates every layer after it. Each lane's card shows how many layers or allotments its
latest build rebuilt.

### What is measured

Every number on the page comes from the same event times (`measure()` in
[dashboard/app.js](dashboard/app.js), and the same definitions in
[tools/summary.py](tools/summary.py)):

- **Time until a change is live**: from the start of an act (the build begins) to the lane's
  first frame of the new version. For Act 1, to the first frame at all.
- **Downtime**: gaps of more than a second without output inside that window. Shorter gaps
  are slow frames on a busy machine, not an outage.
- **Total time**: time until the change is live, summed over the four acts.
- **Total build and push**: build plus push time, summed over every version. The lane card
  also counts the layers or allotments rebuilt across those builds.
- **Downloaded**: bytes the lane pulled over the emulated link.

One traffic run on a MacBook Pro (M4 Pro, 14 CPUs, Apple GPU helper, 200 Mbit/s emulated
link, all four methods at once):

| | Docker | eStargz | 2DFS | MitraFSnap |
|---|---:|---:|---:|---:|
| Act 1 until first frame | 70.2 s | 40.3 s | 24.0 s | **16.6 s** |
| Act 2 (add segmenter) live after | 29.0 s | 29.7 s | 10.3 s | **8.3 s** |
| Act 3 (upgrade detector) live after | 36.2 s | 36.1 s | 13.7 s | **12.6 s** |
| Act 4 (patch unused model) live after | 72.7 s | 43.2 s | 20.2 s | **15.2 s** |
| Total downtime, Acts 2 to 4 | 73.5 s | 26.1 s | 12.8 s | **0 s** |
| Total time, all acts | 208.1 s | 149.3 s | 68.3 s | **52.8 s** |
| Total build and push | 84.5 s | 109.0 s | 41.1 s | **34.6 s** |
| Downloaded | 1475 MB | 679 MB | 227 MB | **173 MB** |

The people run gives the same order: MitraFSnap has no downtime and the lowest total time
(49.7 s, against 209.0 s for Docker, 213.5 s for eStargz and 67.4 s for 2DFS).

These are live-mode numbers: the four methods share one machine's CPU, GPU and link, so treat
them as a demonstration rather than a benchmark. The paper's evaluation measures each method
alone.

## Run it

You need Docker with privileged containers and `/dev/fuse`: Docker Desktop on macOS, or Docker
Engine on Linux (x86_64 or arm64). Plan for about 15 GB of free disk and 8 GB of memory for four
lanes; two lanes need about half.

```sh
git clone https://github.com/mitrafsnap/demo.git
cd demo
tools/accel/run.sh              # macOS only, in its own terminal: runs the models on the Mac's GPU
docker compose up -d --build    # the backend
```

The first build compiles the MitraFSnap snapshotter, the 2DFS builder and the registry from the
`mitrafsnap` repositories and takes a few minutes. Then open **<http://localhost:8080>**:

1. Pick a **Scenario** and the **Methods**. Docker and MitraFSnap are shown by default;
   **Show all** adds eStargz and 2DFS.
2. Click **Act 1** to **Act 4** in order. Each act ends when every lane shows its result.
   Act 1 starts from a clean state every time; the first Act 1 also downloads the models and the
   clip (about 1.3 GB) and builds the base images, which takes several minutes.
3. **Reset** clears all lanes, images and caches without starting a new run.

**Edge link** sets the bandwidth between the registry and the lanes; lower values make the
differences easier to see. To use your webcam, pick the **people** scenario and click **Use my
webcam**. Browsers only allow the camera on `https` or `localhost` pages.

### Stop it

```sh
docker compose down       # stop the backend; models, images and caches are kept
docker compose down -v    # stop it and delete everything it downloaded and built
```

Stop the Apple GPU helper with Ctrl-C in its terminal. `docker compose ps` shows what is running.

### GPU

The **Device** setting is `auto` by default: the lanes run their models on a GPU when the
backend has one, and on the CPU otherwise. `Apple GPU`, `NVIDIA GPU` and `CPU` force one (a
device that isn't found is greyed out); the choice applies from the next Act 1. The status pill
at the top right shows which one is in use.

**Mac (Apple GPU).** Docker Desktop's Linux VM has no GPU, so `tools/accel/run.sh` runs a small
helper natively on macOS that executes the models with ONNX Runtime's CoreML provider. The lanes
still read every model file from their own container mount, so lazy fetching and in-place
refresh work exactly as without it; only the arithmetic moves to the helper. Act 1 compiles every
model once, so no lane pays CoreML's compile time during an update.

**NVIDIA.** On a machine with an NVIDIA GPU and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/):

```sh
docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

The lane runtime is then built with CUDA-enabled ONNX Runtime 1.31 (CUDA 13), so the host needs
an NVIDIA driver with CUDA 13 support. This covers the **DGX Spark** (arm64) and **x86 servers**
such as A100 or H100 machines with a current driver. **Jetson** boards are not covered yet: they
need NVIDIA's Jetson builds of ONNX Runtime and the JetPack libraries instead of the PyPI wheels,
which means a different lane base image. NVIDIA mode has not been tested on hardware yet.

### Run the parts on separate machines

By default everything runs on one machine. Each part can move to its own machine, and every
machine runs the same backend (`docker compose up -d --build`):

- **App backend**: runs the lanes, plays the camera feed and coordinates the acts. The dashboard
  connects to it (the **Backend** field at the top), for example a DGX Spark next to the camera:

  ```sh
  DEMO_BIND=0.0.0.0 DEMO_TOKEN=choose-a-secret docker compose up -d --build
  ```

  Then open `http://<machine>:8080/?token=choose-a-secret`. An SSH tunnel
  (`ssh -L 8080:localhost:8080 <machine>`) also works, and is the easiest way to use your webcam
  with a remote backend.
- **Registry**: under **Machines**, set **Registry** to `host:port` of a registry both the app
  and the builder can reach. The registry of any demo backend works; start that backend
  with `DEMO_REGISTRY_BIND=0.0.0.0` so other machines can reach it.
- **Builder**: set **Builder** to the URL of another demo backend, and **token** to its
  `DEMO_TOKEN` if it has one. The app backend then asks that machine to prepare the base images
  and run every build, and streams each build's phases back into the dashboard.

Edge-link emulation only applies when the registry runs on the app's machine: with a registry
elsewhere, the lanes pull over the real network, **Edge link** shows "real network", and nothing
is throttled. Downloads are still counted per method. Changing a machine applies from the next
Act 1.

### Drive it from a terminal

```sh
tools/demo.py lanes docker estargz twodfs mitrafsnap    # choose methods
tools/demo.py act deploy add-model upgrade patch       # run the four acts
tools/demo.py trace | tools/summary.py                  # per-lane numbers
```

`tools/demo.py set 'scenario="people"'` switches scenarios; `DEMO_BACKEND` and `DEMO_TOKEN`
point it at a remote backend. `tools/record.py` captures the dashboard in a headless browser
(including the intro cards) and `tools/encode.sh` turns the frames into the videos and GIFs.

### Build from local clones

To test changes to the snapshotter, the builder or the registry, clone them next to this
repository and use the override file:

```sh
cp compose.override.example.yaml compose.override.yaml
docker compose up -d --build
```

The override also mounts `controller/`, `dashboard/`, `scenarios/`, `lane/` and `models/` into
the node, so `docker compose restart node` picks up edits without rebuilding.

## How it runs

```text
your machine
├─ tools/accel/run.sh       macOS only: runs the models on the Mac's GPU (CoreML)
└─ Docker
   ├─ registry              2dfs-registry, the MitraFSnap fork: stores all four images
   └─ node                  one privileged container that is a small "edge device":
      ├─ containerd         its own container runtime, separate from Docker's
      ├─ snapshotters       overlayfs (Docker, 2DFS lanes) and the MitraFSnap
      │                     snapshotter (eStargz and MitraFSnap lanes, lazy and refresh)
      ├─ BuildKit, tdfs     the builders: BuildKit for Docker and eStargz, tdfs for 2DFS
      │                     and MitraFSnap
      ├─ controller         HTTP API and dashboard on :8080, runs the acts, plays the
      │                     camera feed, emulates the edge link, records the trace
      └─ lane containers    one per method, all running the same vision app
```

**Why containers inside a container?** The comparison is about how a container runtime pulls,
mounts and updates images, so each method needs a runtime it fully controls: MitraFSnap's
snapshotter, a FUSE mount per lazy image, and refresh on a mounted snapshot. Docker on your
machine can't do that, and on macOS it runs inside a Linux VM anyway. So Docker only starts two
containers, the registry and the node, and the node runs its own containerd with the four
lanes. `docker compose down` stops all of it, lanes included. The lanes don't appear in
`docker ps`, because they belong to the node's containerd; to look at them:

```sh
docker compose exec node ctr -n mitrafsnap tasks ls     # namespaces: docker, estargz, twodfs, mitrafsnap
```

More detail:

- **Same app, four pipelines.** Docker and eStargz build with BuildKit (one layer per model);
  2DFS and MitraFSnap build one allotment per model with `tdfs`. All four start from the same
  lane runtime.
- **No sharing between methods.** Each method runs in its own containerd namespace with content
  sharing turned off, so no lane can reuse another lane's downloads.
- **One link for everyone.** Lanes pull through the controller's proxy: one port per method for
  byte counting, a shared token bucket for the bandwidth. Builders push to the registry directly.
- **Same frames for everyone.** The controller plays the clip (or the webcam) at 10 fps and every
  lane processes the same frames; a lane that isn't serving drops them.
- **The app picks up updates itself.** The lane app reopens `/models/config.json` five times a
  second and reloads any model whose digest changed. Each model runs in its own worker process,
  so the old model keeps serving while the new one loads. Replacement lanes see the new version
  when their container restarts; MitraFSnap lanes see it behind the running mount after
  `ctr-remote refresh`.

### Measuring fairly

- Live mode runs all shown methods at once on one machine; they compete for CPU, GPU, disk and
  the link. That is part of the picture an audience sees, not a clean measurement.
- The Dockerfile adds the models in the order they were introduced. Putting the model that
  changes last would be Docker's best case for that one update, but a Dockerfile can only favor
  one model.
- The eStargz lane uses stock `ctr-remote rpull` behaviour, including its 10 MB default prefetch
  per layer.
- Act 1 clears every lane image, snapshot and cache, and the build caches, so each run starts
  cold.

## Platform notes

- **Docker Desktop (macOS).** Its LinuxKit kernel fails every exec through overlayfs on a FUSE
  layer that supports xattrs. The node detects this kernel and enables the snapshotter's
  `[fuse] disable_xattrs` option, which exists only in the MitraFSnap fork.
- **Raspberry Pi 5.** Works on the CPU; expect a few frames per second per lane with four lanes.

## Troubleshooting

- `docker compose logs node` shows the controller; daemon and lane logs are in `/var/log/node/`
  inside the node (`docker compose exec node tail /var/log/node/lane-docker.log`).
- If an act fails, its error is in the dashboard's event log. Run Act 1 again to start over.
- The dashboard reconnects and reloads by itself if the backend restarts.

## Layout

```text
compose.yaml      backend stack (compose.gpu.yaml adds an NVIDIA GPU)
node/             node image: containerd, snapshotter, BuildKit, tdfs; smoke-test.sh
controller/       API, acts, methods, emulated link, frame source
lane/             vision app and its runtime image
dashboard/        static dashboard (HTML, CSS, JS, no build step)
scenarios/        scenario definitions: models, versions, wording, licenses
models/export/    prepares ONNX models during Act 1 (pins MobileSAM's input shapes)
tools/            CLI, trace summary, screenshots, recording and encoding
tools/accel/      Apple GPU helper for macOS
DESIGN.md         design notes
```

## Credits

Clips, from Wikimedia Commons:
[DiagonalCrosswalkYongeDundas.webm](https://commons.wikimedia.org/wiki/File:DiagonalCrosswalkYongeDundas.webm)
(CC0) and
[AFN InFocus- DDG117 holds Sevillanas dance Classes](https://commons.wikimedia.org/wiki/File:AFN_InFocus-_DDG117_holds_Sevillanas_dance_Classes_(960160).webm)
(public domain, U.S. Department of Defense).

Models: YOLOX (Megvii, Apache-2.0); MobileSAM (Apache-2.0; ONNX export by Acly on Hugging Face,
MIT); YuNet (OpenCV Zoo, MIT); RTMPose-m (OpenMMLab, Apache-2.0); FER+, ResNet-152 v2, VGG-16 and
VGG-16-BN from the ONNX Model Zoo (FER+ MIT, the others Apache-2.0). The dashboard lists the
assets of the selected scenario.

Font: [CMU Sans Serif](https://ctan.org/pkg/cm-unicode) (SIL Open Font License,
`dashboard/fonts/OFL.txt`). Method colors follow the paper's figures.
