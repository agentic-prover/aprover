#!/usr/bin/env bash
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"
$P run --rm --userns=keep-id -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc '
cd /w/harness
for v in orig mim; do
  echo "=========== W_$v ==========="
  echo "  contract violations: $(ls W_$v/*.contract 2>/dev/null | wc -l)"
  for f in W_$v/*.contract; do
    [ -e "$f" ] || continue
    echo "  --- $f ---"; cat "$f" | sed "s/^/    /"
    k="${f%.contract}.ktest"
    echo "  --- witness $k ---"; ktest-tool "$k" | sed "s/^/    /"
  done
done'
