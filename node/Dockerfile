# syntax=docker/dockerfile:1
#
# Demo node: containerd + MitraFSnap snapshotter + buildkitd + tdfs.
# The demo's lane containers run in this containerd, not in the host engine.
#
# Named build contexts (set in compose.yaml):
#   snapshotter  mitrafsnap/stargz-snapshotter
#   builder      mitrafsnap/2dfs-builder

ARG GO_VERSION=1.25
ARG UBUNTU_VERSION=24.04

FROM --platform=$BUILDPLATFORM golang:${GO_VERSION} AS go
ARG TARGETOS TARGETARCH
ENV CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH}

FROM go AS build-snapshotter
WORKDIR /src
RUN --mount=from=snapshotter,target=/src,rw \
    --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    cd cmd && \
    go build -trimpath -ldflags "-s -w" -o /out/containerd-stargz-grpc ./containerd-stargz-grpc && \
    go build -trimpath -ldflags "-s -w" -o /out/ctr-remote ./ctr-remote

FROM go AS build-tdfs
WORKDIR /src
RUN --mount=from=builder,target=/src,rw \
    --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    go build -trimpath -ldflags "-s -w" -o /out/tdfs ./internal/tdfs.go

FROM ubuntu:${UBUNTU_VERSION}
ARG TARGETARCH
ARG CONTAINERD_VERSION=2.2.1
ARG RUNC_VERSION=1.3.4
ARG NERDCTL_VERSION=2.2.1

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl fuse3 gnupg iptables iproute2 jq python3 tini \
      python3-aiohttp python3-numpy python3-opencv && \
    rm -rf /var/lib/apt/lists/*

# nerdctl-full bundles containerd, buildkitd, buildctl, CNI plugins and runc;
# containerd and runc are then pinned to the versions used in the paper.
RUN curl -fsSL "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-full-${NERDCTL_VERSION}-linux-${TARGETARCH}.tar.gz" \
      | tar -xz -C /usr/local && \
    curl -fsSL "https://github.com/containerd/containerd/releases/download/v${CONTAINERD_VERSION}/containerd-${CONTAINERD_VERSION}-linux-${TARGETARCH}.tar.gz" \
      | tar -xz -C /usr/local && \
    curl -fsSL -o /usr/local/sbin/runc "https://github.com/opencontainers/runc/releases/download/v${RUNC_VERSION}/runc.${TARGETARCH}" && \
    chmod 755 /usr/local/sbin/runc

# nvidia-container-cli gives lane containers a passed-in GPU (ctr run --gpus);
# unused without one.
RUN curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit.gpg && \
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit.gpg] https://#' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list && \
    apt-get update && apt-get install -y --no-install-recommends nvidia-container-toolkit && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build-snapshotter /out/ /usr/local/bin/
COPY --from=build-tdfs /out/ /usr/local/bin/
COPY etc/ /etc/
COPY entrypoint.sh /usr/local/bin/node-entrypoint
# The rest of the demo comes from the repo root (compose context "demo").
COPY --from=demo controller/ /opt/demo/controller/
COPY --from=demo lane/ /opt/demo/lane/
COPY --from=demo scenarios/ /opt/demo/scenarios/
COPY --from=demo models/ /opt/demo/models/
COPY --from=demo dashboard/ /opt/demo/dashboard/

VOLUME ["/var/lib/containerd", "/var/lib/containerd-stargz-grpc", "/var/lib/buildkit", "/var/lib/demo"]
EXPOSE 8080
ENTRYPOINT ["tini", "--", "node-entrypoint"]
