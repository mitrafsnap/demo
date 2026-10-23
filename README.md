# MitraFSnap live demo

A vision application deployed and updated four ways at once: Docker, eStargz, 2DFS and
[MitraFSnap](https://github.com/mitrafsnap). Every method builds the same image, deploys it to
the same emulated edge link, and rolls out the same updates while a camera stream keeps
running. The dashboard shows each method's video, where it is in the workflow, and what it
cost.

![Rolling out an update: replacement lanes go blind, MitraFSnap keeps serving](docs/demo-highlight.gif)

Above: Act 5 at real speed. Docker, eStargz and 2DFS stop their containers to roll out v2;
MitraFSnap refreshes the running container and keeps serving.

▶ **[Full end-to-end run (MP4, 1080p, 2 min 8 s)](docs/demo.mp4)**: Acts 1 to 6 with all four
methods, played at 2× speed. The metrics in the video are real time.

## What the demo shows

There are two scenarios, picked in the dashboard's **Scenario** menu. Both run the same acts.

- **traffic**: a city camera at a busy crossing (recorded clip). Its image holds a small
  detector the camera uses, a large segmenter it turns on later, and two large models it never
  uses. v2 ships a stronger detector that also recognizes bicycles, buses and trucks.
- **people**: a camera that watches people, fed by **your webcam** (or a recorded clip until you
  turn it on). v1 finds faces, Act 3 turns on body pose, and v2 ships an expression model that
  labels smiles. The same two unused models are in the image.

The acts, for the traffic scenario:

| Act | What happens | What to watch |
|---|---|---|
| 1 Build v1 | each method builds and pushes the image | independent allotments build in parallel (2DFS, MitraFSnap) |
| 2 Deploy | the camera needs only the detector | Docker pulls 1.1 GB before the first frame; lazy methods start from about 100 MB |
| 3 Enable segmenter | Mask R-CNN instance masks, a model the camera did not fetch | lazy methods fetch it on first use; 2DFS has to redeploy; Docker already paid for it in act 2 |
| 4 Build v2 | a stronger detector with new classes | only the changed allotments are rebuilt |
| 5 Roll out v2 | replace vs refresh in place | replacement lanes go blind; MitraFSnap keeps serving and switches to v2 |
| 6 Patch unused model | v3 changes a 528 MB model the camera never loads | Docker rebuilds and re-pulls it; MitraFSnap refreshes in milliseconds and fetches nothing |

Each lane reports build and push time, bytes over the link, time to first frame, time until
a new version is live, and how long it produced no output ("blind").

The run in the video (Apple M4 Pro, Docker Desktop, all four methods at once, 200 Mbit/s
link), as reported by `tools/summary.py`:

| | Docker | eStargz | 2DFS | MitraFSnap |
|---|---|---|---|---|
| Build + push v1 | 18.4 s | 24.0 s | 8.8 s | 5.9 s |
| First frame after deploy | 64.8 s | 11.4 s | 53.5 s | 14.1 s |
| Blind during v2 rollout | 7.5 s | 5.8 s | 8.7 s | **0 s** |
| Blind during v3 patch | 58.6 s | 29.4 s | 2.4 s | **0 s** |
| Downloaded over the run | 1.61 GB | 991 MB | 363 MB | **326 MB** |

These are live-mode numbers: the four methods share one machine's CPU and link, so treat them
as a demonstration, not a benchmark. The paper's evaluation measures each method alone.

## Run it

You need Docker with privileged containers and `/dev/fuse`: Docker Desktop on macOS, or Docker
Engine on Linux (x86_64 or arm64, e.g. a workstation, a DGX Spark or a Raspberry Pi 5). Plan for
about 15 GB of free disk and 8 GB of memory for four lanes; two lanes need about half.

```sh
git clone https://github.com/mitrafsnap/demo.git
cd demo
docker compose up -d --build
```

The first build compiles the MitraFSnap snapshotter, the 2DFS builder and the registry from
the `mitrafsnap` repositories and takes a few minutes. Then open
**http://localhost:8080** and click through the acts from left to right:

1. **Setup: Prepare** downloads the models and the clip (about 1.5 GB, once), exports the
   segmentation model to ONNX, and builds the lane runtime in its three base formats. The first
   time takes several minutes; later it takes seconds.
2. **Act 1 to Act 6**, in order. Each act ends when every lane shows its result.
3. **Reset** clears all lanes, images and caches so the next run starts cold. Assets and base
   images are kept.

To use your webcam, pick the **people** scenario and click **Use my webcam**. The browser sends
its camera frames to the backend, so the camera never has to be attached to the machine running
the backend. Browsers only allow the camera on `https` or `localhost` pages: open the dashboard
at `http://localhost:8080`, or through an SSH tunnel when the backend runs elsewhere. Without
the webcam the scenario plays its recorded clip.

Docker and MitraFSnap are shown by default. Tick eStargz and 2DFS, or click **Show all**, before
Act 1 to compare all four. **Edge link** sets the shared bandwidth between the registry and the
lanes; lower values make the differences easier to see.

Everything runs in containers. On macOS they run inside Docker Desktop's own VM, so no extra VM
is needed.

### GPU

On a machine with an NVIDIA GPU and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
(a DGX Spark, an x86 workstation), pass the GPU into the backend:

```sh
docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

The **Device** setting in the dashboard is `auto` by default: the lanes run their models on the
GPU when the backend has one, and on the CPU otherwise. Choose `CPU` or `GPU` to force one, then
run **Prepare** again, because the lane runtime is built per device. The status pill at the top
right shows which one is in use. GPU mode installs CUDA-enabled ONNX Runtime with its CUDA and
cuDNN libraries, so its runtime image is several GB larger; that changes the absolute pull
numbers. The GPU runtime uses ONNX Runtime 1.31, built for CUDA 13, so the host needs an NVIDIA driver
with CUDA 13 support (the DGX Spark's does). Docker Desktop on macOS has no GPU for containers,
so a Mac always uses the CPU. GPU mode has not been tested on NVIDIA hardware yet.

### Run the backend on another machine

The dashboard only talks to the backend over HTTP, so the backend can run anywhere, for
example on a DGX Spark or a Raspberry Pi 5 next to the camera.

On that machine:

```sh
DEMO_BIND=0.0.0.0 DEMO_TOKEN=choose-a-secret docker compose up -d --build
```

Then open `http://<machine>:8080/?token=choose-a-secret`, or open any copy of the dashboard and
enter the backend URL and token at the top. Without `DEMO_BIND` the backend only listens on
localhost; an SSH tunnel (`ssh -L 8080:localhost:8080 <machine>`) works as well, and is the
easiest way to use your webcam with a remote backend.

### Drive it from a terminal

```sh
tools/demo.py lanes docker estargz twodfs mitrafsnap     # choose methods
tools/demo.py act prepare reset build-v1 deploy enable build-v2 update patch
tools/demo.py trace > run.json                          # events of the current run
tools/summary.py run.json                               # per-lane numbers
```

Set `DEMO_BACKEND` and `DEMO_TOKEN` to drive a remote backend. `tools/demo.py set
'scenario="people"'` switches scenarios. `tools/record.py` captures the dashboard in a
headless browser while the acts run, and `tools/encode.sh` turns the frames into the video and
GIF above. `tools/webcam_check.py`
checks the webcam path with Chromium's fake camera.

### Build from local clones

To test changes to the snapshotter, the builder or the registry, clone them next to this
repository and use the override file:

```sh
cp compose.override.example.yaml compose.override.yaml
docker compose up -d --build
```

The override also mounts `controller/`, `dashboard/`, `scenarios/` and `lane/` into the node,
so `docker compose restart node` picks up edits without rebuilding.

## How it works

```text
docker compose
  registry     2dfs-registry (MitraFSnap fork): partition selection becomes prefetch hints
  node         privileged container with its own containerd, the MitraFSnap snapshotter,
               BuildKit and tdfs; the lane containers run here, not in the host's Docker
    controller HTTP API, act orchestration, frame source, emulated link, trace recorder
    lanes      one container per method, all running the same lane app
```

- **Same app, four pipelines.** Docker and eStargz build with BuildKit (one layer per model,
  ordered by how often a model changes, which is Docker's best case for the v2 update). 2DFS and
  MitraFSnap build one allotment per model with `tdfs`. All four start from the same lane runtime.
- **No sharing between methods.** Each method runs in its own containerd namespace with content
  sharing turned off, so no lane can reuse another lane's downloads.
- **One link for everyone.** Lanes pull through the controller's proxy: one port per method for
  byte counting, and a shared token bucket for the bandwidth. Builders push to the registry
  directly.
- **Same frames for everyone.** The controller plays the clip (or the webcam) at 10 fps and
  every lane processes the same frames. A lane that is not serving drops them.
- **The app picks up updates itself.** The lane app reopens `/models/config.json` five times a
  second and reloads any model whose digest changed. Each model runs in its own worker process,
  so the old model keeps serving while the new one loads. Replacement lanes see the new
  version when their container restarts; MitraFSnap lanes see it behind the running mount after
  `ctr-remote refresh`.

### Measuring fairly

- Live mode runs all shown methods at the same time on one machine. They compete for CPU, disk
  and the link, which is part of the picture an audience sees but not a clean measurement.
- Docker's layer order favours the v2 update (the detector is near the top). Act 6 updates the
  bottom layer instead, which is where Docker's linear layers cost the most.
- The eStargz lane uses stock `ctr-remote rpull` behaviour, including its 10 MB default prefetch
  per layer.
- Every reset clears all lane images, snapshots and the snapshotter's caches, and Act 1 clears
  the build caches, so each run starts cold.

## Platform notes

- **Docker Desktop (macOS).** Its LinuxKit kernel fails every exec through overlayfs on a FUSE
  layer that supports xattrs. The node detects this kernel and enables the snapshotter's
  `[fuse] disable_xattrs` option, which only exists in the MitraFSnap fork.
- **DGX Spark.** Start with `compose.gpu.yaml` (see [GPU](#gpu)).
- **Raspberry Pi 5.** Works; expect a few frames per second per lane with four lanes.

## Troubleshooting

- `docker compose logs node` shows the controller; daemon logs are in `/var/log/node/` inside
  the node (`docker compose exec node tail /var/log/node/stargz.log`).
- If an act fails, the error is in the dashboard's event log. **Reset** and start again from Act 1.
- `docker compose down -v` removes everything, including downloaded models.

## Layout

```text
compose.yaml      backend stack
node/             node image: containerd, snapshotter, BuildKit, tdfs; smoke-test.sh
controller/       API, acts, methods, emulated link, frame source
lane/             lane app and its runtime image
dashboard/        static dashboard (HTML, CSS, JS, no build step)
scenarios/        scenario definitions: models, versions, licenses
models/export/    exports torchvision models to ONNX during Prepare
tools/            CLI, trace summary, screenshots, recording and encoding
DESIGN.md         design notes
```

## Credits

Clips, from Wikimedia Commons:
[DiagonalCrosswalkYongeDundas.webm](https://commons.wikimedia.org/wiki/File:DiagonalCrosswalkYongeDundas.webm)
(CC0) and
[AFN InFocus- DDG117 holds Sevillanas dance Classes](https://commons.wikimedia.org/wiki/File:AFN_InFocus-_DDG117_holds_Sevillanas_dance_Classes_(960160).webm)
(public domain, U.S. Department of Defense).

Models: YOLOX (Megvii, Apache-2.0); Mask R-CNN ResNet-50 FPN v2 (torchvision, BSD-3-Clause;
exported to ONNX during **Prepare** by `models/export`); YuNet (OpenCV Zoo, MIT); RTMPose-m
(OpenMMLab, Apache-2.0); FER+, ResNet-152 v2, VGG-16 and VGG-16-BN from the ONNX Model Zoo (FER+
MIT, the others Apache-2.0). The dashboard lists the assets of the selected scenario.

Font: [CMU Sans Serif](https://ctan.org/pkg/cm-unicode) (SIL Open Font License,
`dashboard/fonts/OFL.txt`). Method colors follow the paper's figures.
