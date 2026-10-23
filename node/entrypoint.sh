#!/bin/bash
# Starts the snapshotter, containerd, buildkitd and the demo controller.
# The snapshotter runs in a restart loop because the controller kills it to
# drop its caches between runs (methods.reset_snapshotter).
set -eu

mkdir -p /run/containerd-stargz-grpc /var/log/node /var/lib/demo
modprobe fuse 2>/dev/null || true

# Docker Desktop's LinuxKit kernel fails exec through overlayfs on FUSE layers
# unless the snapshotter disables xattrs (see the option's doc).
CONF=/etc/containerd-stargz-grpc/config.toml
if uname -r | grep -q linuxkit && ! grep -q disable_xattrs "$CONF"; then
  sed -i 's/^\[fuse\]$/[fuse]\n  disable_xattrs = true/' "$CONF"
fi

(
  while true; do
    containerd-stargz-grpc --log-level=info \
      --address=/run/containerd-stargz-grpc/containerd-stargz-grpc.sock
    echo "snapshotter exited, restarting"
    sleep 0.5
  done
) >>/var/log/node/stargz.log 2>&1 &
containerd >/var/log/node/containerd.log 2>&1 &

until ctr version >/dev/null 2>&1; do sleep 0.2; done
buildkitd --config /etc/buildkit/buildkitd.toml >/var/log/node/buildkitd.log 2>&1 &

echo "node ready: $(uname -m), containerd $(containerd --version | awk '{print $3}')"
cd /opt/demo/controller
exec python3 main.py
