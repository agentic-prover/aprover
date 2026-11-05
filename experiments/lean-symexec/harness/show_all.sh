#!/usr/bin/env bash
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"
$P run --rm --userns=keep-id -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc '
cd /w/harness
for t in free_mim focus_mim nmfree_mim nmfocus_mim; do
  for f in V_$t/*.contract; do
    [ -e "$f" ] || continue
    echo "=== $t : $f"
    head -1 "$f" | sed "s/^/    /"
    ktest-tool "${f%.contract}.ktest" | grep "hex" | sed "s/^/    /"
  done
done
echo "=== instruction counts ==="
for t in free_orig free_mim focus_orig focus_mim nmfree_orig nmfree_mim nmfocus_orig nmfocus_mim; do
  printf "%-14s %s\n" "$t" "$(grep -oE "total instructions = [0-9]+" V_$t.out|tail -1)"
done'
