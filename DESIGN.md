# MitraFSnap live demo: design

Status: implemented for both scenarios, 2026-10-23. README.md describes how to run it.

## Goal

Show MitraFSnap's three strengths on a running application, next to the
baselines of the evaluation:

1. Build: an update rebuilds only the allotments it touches.
2. Lazy pulling with partition selection: a worker prefetches its selected
   models, starts early, and still reads the others on demand from the same
   image.
3. In place update: a new version reaches the running container without
   stopping it, and unchanged cached data stays local.

Every scenario is shaped so that each act exercises one of these.

## Lanes

A lane is one method running the same application.

- Shown by default: Docker and MitraFSnap.
- Optional: eStargz and 2DFS, added individually or with "show all".
- The lane set is chosen in the dashboard before a run. Hidden lanes don't
  run, so they don't compete for resources.

## Scenarios

Both scenarios use the same stack, dashboard and acts. They differ in the image
contents and the frame source. Models are ONNX, chosen for the demo and for a
redistributable license (Apache, MIT, BSD; no AGPL weights). The concrete
picks live in `scenarios/<name>/models.yaml`.

### S1 "Traffic camera" (recorded clip)

Image `traffic-vision`, one fixed slot (allotment) per model.

| Slot | Role in the demo | v1 | v2 |
|---|---|---|---|
| runtime | python, onnxruntime, opencv, lane app | | |
| detector | used from the start (small) | cars, people | stronger model, adds bicycles, motorcycles, buses |
| segmenter | enabled in act 2 (large) | road / crosswalk mask | |
| classifier | never enabled (large) | vehicle type | |
| aux | never enabled (large) | second backbone | |
| config | labels, thresholds, overlay style | | changed with v2 |

The bundle is deliberately weighted toward models that a given camera does not
use, as in a real multi-model deployment. That makes eager pulling visibly
expensive and lazy pulling visibly cheap.

Input: a looping traffic clip with a redistributable license.

### S2 "People camera" (live webcam)

Image `people-vision`, same slot pattern: a small face detector used from the
start, a large pose or segmentation model enabled in act 2, unused large
models, and an expression (smile) model as the v2 update.

Input: the webcam of whatever browser shows the dashboard, sent to the backend
over the API (see below). No camera device has to reach the backend host.

## Acts

| Act | Highlights | Docker | eStargz | 2DFS | MitraFSnap |
|---|---|---|---|---|---|
| 1 Build v1 | build | BuildKit, linear layers | BuildKit + eStargz | `tdfs build`, independent allotments | `tdfs build --enable-stargz`, independent lazy allotments |
| 2 Deploy, detector only | lazy pull | eager pull of the full image | lazy pull | eager pull of selected allotments | prefetch selected allotments, rest lazy; first frame earliest |
| 3 Enable segmenter | lazy pull | already local, paid in act 2 | fetched on first use | not in the pulled image: redeploy with a new selection | fetched on first use from the running image |
| 4 Build v2 | build | rebuild invalidates layers above the detector | same as Docker | rebuild detector + config allotments only | rebuild detector + config allotments only |
| 5 Deploy v2 | in place update | stop, pull, run, reread | stop, pull, run, reread (cold cache) | stop, pull, run, reread | watch, refresh, app reopens files; no dropped frames |

Reset restores v1 on all lanes so the demo can be repeated. In act 3, 2DFS
shows "redeploy required" and redeploys when the presenter confirms.

## Modes

- Live mode (default): all enabled lanes run at once. Each lane gets its own
  cpuset, its own registry repository and capped inference fps.
- Fair mode (optional): each enabled method runs the act alone, and the
  dashboard plays the recordings in lockstep. Used for numbers and for the
  landing page.

The dashboard labels which mode produced what is on screen.

## Dashboard

- One column per enabled lane: annotated video, frame timeline (gaps are time
  without output), and counters: build time, bytes pulled, time to first
  frame, downtime, frames dropped, objects missed, time until v2 is live.
- A workflow diagram based on the paper's Figure 2: build → push → registry →
  pull/prefetch → run → (update) rebuild → push → replace (stop, pull, run,
  reread) or refresh (watch, TOC diff, reader swap, invalidate, reopen). Each
  lane has a colored marker that moves as its events arrive, so the audience
  sees which phase every method is in.
- An allotment grid per lane that lights up rebuilt and fetched allotments.
- Controls: scenario, lanes, mode, act buttons, reset.
- Every run is recorded as an event trace (JSON).

## Backend and frontend

The dashboard is a static web app that talks to the backend only through its
API. The backend URL is a setting, so the same dashboard can drive:

- a backend on the presenter's laptop (`http://localhost:8080`),
- a backend on a remote machine (DGX Spark, CloudLab, a Pi 5 on the LAN),
- no backend at all: trace replay, which is what mitrafsnap.github.io shows.

API (v1, sketch):

| Endpoint | Purpose |
|---|---|
| `GET /api/info` | backend version, arch, accelerators, scenarios, available lanes |
| `POST /api/runs` | start a run: scenario, lanes, mode |
| `POST /api/runs/{id}/acts/{act}` | trigger an act |
| `POST /api/runs/{id}/reset` | back to v1 |
| `GET /api/runs/{id}/events` | server-sent events: phases, counters, frame stats |
| `GET /api/runs/{id}/lanes/{lane}/video` | annotated frames (MJPEG first; WebRTC later if needed) |
| `WS /api/runs/{id}/camera` | webcam frames from the browser (S2) |
| `GET /api/runs/{id}/trace` | recorded trace |

Remote use: the backend enables CORS for the dashboard origin and requires a
bearer token when it listens on anything other than localhost.

## Deployment: containers, no VM

The backend runs as containers (Docker Compose). Containers on macOS already
run inside Docker Desktop's Linux VM, so no separate VM is needed. Checked on
Docker Desktop 29.1 (arm64, LinuxKit 6.12): a privileged container gets
`/dev/fuse`, FUSE, overlayfs and cgroup v2 controllers.

```
docker compose up
  node        privileged: containerd, runc, MitraFSnap snapshotter (also serves
              the eStargz lane), buildkitd, tdfs; lane containers run inside it
  registry    2dfs-registry (MitraFSnap fork), :5000
  controller  API, orchestration, event bus, trace recorder, frame source
  dashboard   static files (also loadable from anywhere else)
```

The demo's lane containers run in the node's own containerd, not in the host's
Docker. That is what lets each method use its own snapshotter and lets refresh
act on a real mounted snapshot. The host only needs Docker (or any engine that
supports privileged containers and `/dev/fuse`).

Targets: macOS with Docker Desktop (arm64), Raspberry Pi 5 (arm64), DGX Spark
(arm64, CUDA), x86 servers. Images are built for `linux/arm64` and
`linux/amd64`. ONNX Runtime uses CPU by default and CUDA where available. On a
Pi 5, several concurrent lanes run at low fps; the dashboard shows the
measured fps.

## Constraints from the paper

- Refresh needs the same layer count and media type per position, so the image
  has fixed slots for every model from v1 on, and v2 only changes bytes inside
  the detector and config slots.
- Refresh preserves the mount, not application state: the lane app has to
  reopen files to see the new version.

## Repository layout (planned)

```
compose.yaml          backend stack
node/                 node image: containerd, snapshotter, buildkitd, tdfs
controller/           API, orchestration, event bus, frame source
lane/                 lane app (frames in, annotated frames + events out)
dashboard/            static web app (live and replay)
scenarios/traffic/    models.yaml, 2dfs.json, Dockerfiles, v1/v2 contents
scenarios/people/
models/               ONNX export and download scripts
traces/               recorded runs for the landing page
```
