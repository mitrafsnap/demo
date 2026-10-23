# MitraFSnap live demo: design

Status: draft, 2026-10-23.

## Goal

Show the paper's three claims on a running application, with the four methods
of the evaluation (Docker, eStargz, 2DFS, MitraFSnap) side by side and at the
same time:

1. Independent rebuild: an update rebuilds only the allotments it touches.
2. Partition selection: a worker prefetches its selected models and still reads
   the others on demand from the same image.
3. In place refresh: an update reaches the running container without stopping
   it, and unchanged cached data stays local.

## Scenarios

Both scenarios use the same stack, dashboard and three acts. They differ only
in the image contents and the video source.

### S1 "Traffic camera" (recorded video)

Image `traffic-vision`, one fixed slot (allotment) per model:

| Slot | Content | v1 | v2 |
|---|---|---|---|
| runtime | python, onnxruntime, opencv, lane app | | |
| detector | object detector | cars, people | + bicycles, motorcycles, buses (stronger model) |
| segmenter | road / crosswalk segmentation | | |
| classifier | vehicle type classifier | | |
| aux | second classifier | | |
| config | labels, thresholds, overlay style | | |

Input: a looping traffic clip with a redistributable license.

### S2 "People camera" (live webcam)

Image `people-vision`, same slot structure:

| Slot | Example content |
|---|---|
| face | face detector |
| expression | smile / expression classifier (v2 is the update) |
| pose | body keypoints |
| hands or background | hand landmarks or person segmentation |

Input: the presenter's webcam, captured in the dashboard browser with
`getUserMedia` and sent to the demo node. This works the same whether the
node is a Lima VM on a Mac, a Pi 5 or a DGX Spark, because no camera device
has to be passed into the VM.

### Model licensing

The repo will be public, so models must be redistributable. Prefer
Apache/MIT/BSD models (e.g. YOLOX, RTMDet/RTMPose, YuNet, FER+,
torchvision ResNet/DeepLabV3), exported to ONNX. Avoid AGPL weights.

## The three acts

Each act runs on all four lanes at once.

| Act | Docker | eStargz | 2DFS | MitraFSnap |
|---|---|---|---|---|
| 1 Build v1 | BuildKit, linear layers | BuildKit + eStargz | `tdfs build`, independent allotments | `tdfs build --enable-stargz`, independent lazy allotments |
| 1 Deploy, detector only | eager pull of full image | lazy pull | eager pull of selected allotments | prefetch selected allotments, rest lazy |
| 2 Enable segmenter | already local (paid at deploy) | lazy fetch on first use | not in the image the worker pulled: redeploy with a new selection | lazy fetch on first use from the running image |
| 3 Build v2 | rebuild invalidates layers above the detector | same as Docker | rebuild detector allotment only | rebuild detector allotment only |
| 3 Deploy v2 | stop, pull, run, reread | stop, pull, run, reread (cold cache) | stop, pull, run, reread | watch, refresh, app reopens files |

A reset action restores v1 on all lanes so the demo can be repeated.

## Dashboard

One web page, served by the controller on the demo node.

- Four lane columns, one per method. Each shows the annotated video, a frame
  timeline (gaps are time without output), and counters: build time, bytes
  pulled, downtime, frames dropped, objects missed, time until v2 is live.
- A workflow diagram based on the paper's Figure 2:
  build → push → registry → pull/prefetch → run → (update) rebuild → push →
  replace (stop, pull, run, reread) or refresh (watch, TOC diff, reader swap,
  invalidate, reopen). Each lane has a colored marker that moves along the
  diagram as its events arrive, so the audience sees which phase every method
  is in.
- An allotment grid per lane that lights up rebuilt and fetched allotments.
- Act buttons: Build v1 and deploy, Enable segmenter, Publish v2, Reset.
- Every run is recorded as an event trace (JSON). The landing page replays
  traces, so the online version needs no live backend.

"Online": the live dashboard runs on the demo node and can be reached over the
LAN or a tunnel. The public, always-available version is the trace replay on
mitrafsnap.github.io.

## Fairness when lanes run concurrently

Four builds, pulls and inference loops on one machine compete for CPU, disk
and network, which distorts the comparison. Two modes:

- Live mode: all lanes run at once. Each lane gets its own cpuset, its own
  registry repository and capped inference fps. Good for audiences.
- Fair mode: each method runs the same act alone, and the dashboard plays the
  four recorded traces in lockstep. Good for numbers and for the landing page.

The dashboard labels which mode produced what is on screen.

## Architecture

```
demo node (Lima VM on Mac | Pi 5 | DGX Spark | x86 server)
  controller        web UI + API, act orchestration, event bus, trace recorder
  builders          buildkit, buildkit+estargz, tdfs, tdfs --enable-stargz
  2dfs-registry     :5000, MitraFSnap fork (partition selection = prefetch)
  containerd        overlayfs (Docker, 2DFS lanes), stargz snapshotter
                    (eStargz lane), MitraFSnap snapshotter (MitraFSnap lane)
  4 lane containers same lane app, different image format
  frame source      file loop (S1) or webcam frames from the browser (S2)
```

The lane app reads frames from the frame source, runs the enabled models,
streams annotated frames and events to the controller, and reopens a model
when the model's version file changes. As in the paper, the application
decides when to reload.

Controller events carry timestamps, the lane, the phase and byte counters
(from snapshotter metrics and registry logs). Phase events drive the
workflow diagram markers.

## Portability

- arm64 (Mac VM, Pi 5, DGX Spark) and amd64 (CloudLab, servers).
- One `install.sh` that detects arch (generalized from
  `evaluation/node-setup/vm-setup.sh`, which hardcodes amd64).
- `lima.yaml` for macOS.
- ONNX Runtime execution providers: CPU by default, CUDA on DGX Spark.
- Images built for `linux/arm64` and `linux/amd64`.
- On a Pi 5, four concurrent lanes will run at low fps; the dashboard shows
  the measured fps.

## Constraints from the paper

- Refresh needs the same layer count and media type per position, so the image
  has fixed slots for every model from v1 on, and v2 only changes bytes inside
  the detector and config slots.
- Refresh preserves the mount, not application state: the lane app has to
  reopen files to see the new version.

## Repository layout (planned)

```
install.sh, lima.yaml       node setup
controller/                 API, orchestration, event bus, web dashboard
lane/                       lane app (frames in, annotated frames + events out)
models/                     ONNX export scripts, per-scenario slot definitions
scenarios/traffic, people   2dfs.json, Dockerfiles, v1/v2 contents
traces/                     recorded runs for the landing page
```

## Open questions

- Traffic clip and its license.
- Exact models per slot, and the v2 detector.
- Whether 2DFS in act 2 should redeploy automatically or show "redeploy
  required" and let the presenter trigger it.
