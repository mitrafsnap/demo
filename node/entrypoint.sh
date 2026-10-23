#!/bin/bash
# Starts the snapshotter, containerd and buildkitd, then waits on all three.
set -eu

mkdir -p /run/containerd-stargz-grpc /var/log/node
modprobe fuse 2>/dev/null || true

# Docker Desktop's LinuxKit kernel fails exec through overlayfs on FUSE layers
# unless the snapshotter disables xattrs (see the option's doc).
CONF=/etc/containerd-stargz-grpc/config.toml
if uname -r | grep -q linuxkit && ! grep -q disable_xattrs "$CONF"; then
  sed -i 's/^\[fuse\]$/[fuse]\n  disable_xattrs = true/' "$CONF"
fi

containerd-stargz-grpc --log-level=info --address=/run/containerd-stargz-grpc/containerd-stargz-grpc.sock \
  >/var/log/node/stargz.log 2>&1 &
containerd >/var/log/node/containerd.log 2>&1 &

until ctr version >/dev/null 2>&1; do sleep 0.2; done
buildkitd --config /etc/buildkit/buildkitd.toml >/var/log/node/buildkitd.log 2>&1 &

echo "node ready: $(uname -m), containerd $(containerd --version | awk '{print $3}')"
wait -n
echo "a node daemon exited; see /var/log/node" >&2
exit 1
