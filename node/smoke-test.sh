#!/bin/bash
# End-to-end check of the MitraFSnap path inside the node container:
# tdfs build (lazy allotments) -> partial lazy pull -> run -> refresh in place.
#   docker compose exec node bash /smoke/smoke-test.sh
set -euo pipefail

REG=registry:5000
W=/tmp/smoke
rm -rf "$W" && mkdir -p "$W" && cd "$W"

step() { printf '\n== %s\n' "$*"; }
fetched() { curl -s localhost:8234/metrics | awk '/^stargz_fs_operation_count|bytes_served|fetched/ {print}' | head -5; }

step "base image as zstd:chunked"
nerdctl pull -q alpine:3.20 >/dev/null
nerdctl image convert --zstdchunked --oci alpine:3.20 $REG/base/alpine:3.20-zstd >/dev/null
nerdctl push -q --insecure-registry $REG/base/alpine:3.20-zstd >/dev/null

step "v1 artifacts: two 32 MiB models + small config"
mkdir -p a1 a2 a3
head -c $((32<<20)) /dev/urandom > a1/model_a.bin
head -c $((32<<20)) /dev/urandom > a2/model_b.bin
echo '{"version": 1}' > a3/config.json
cat > 2dfs.json <<J
{"allotments": [
  {"src": ["a1/model_a.bin"], "dst": ["/model_a.bin"], "row": 0, "col": 0},
  {"src": ["a2/model_b.bin"], "dst": ["/model_b.bin"], "row": 0, "col": 1},
  {"src": ["a3/config.json"], "dst": ["/config.json"], "row": 0, "col": 2}
]}
J

step "tdfs build v1"
time tdfs build --enable-stargz --use-zstd --force-http -f 2dfs.json $REG/base/alpine:3.20-zstd $REG/demo/smoke:v1
tdfs image push --force-http $REG/demo/smoke:v1

step "lazy pull of v1 selecting only col 0 (model_a)"
time ctr-remote images rpull --plain-http "$REG/demo/smoke:v1--0.0.0.0"
ctr-remote run --detach --snapshotter=stargz "$REG/demo/smoke:v1--0.0.0.0" smoke sleep infinity
ctr tasks exec --exec-id r1 smoke sh -c 'ls -la /model_a.bin /model_b.bin; cat /config.json; md5sum /model_a.bin /model_b.bin'

step "v2: change config only, rebuild"
echo '{"version": 2}' > a3/config.json
time tdfs build --enable-stargz --use-zstd --force-http -f 2dfs.json $REG/base/alpine:3.20-zstd $REG/demo/smoke:v2
tdfs image push --force-http $REG/demo/smoke:v2

step "refresh running container v1 -> v2"
time ctr-remote refresh --plain-http "$REG/demo/smoke:v1--0.0.0.0" "$REG/demo/smoke:v2--0.0.0.0"
ctr tasks exec --exec-id r2 smoke sh -c 'cat /config.json; md5sum /model_a.bin'
ctr tasks ls

step "cleanup"
ctr tasks kill -s KILL smoke; sleep 1; ctr tasks rm smoke; ctr containers rm smoke
echo "SMOKE OK"
