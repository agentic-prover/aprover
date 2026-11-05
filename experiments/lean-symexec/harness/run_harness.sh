#!/usr/bin/env bash
# Build and run the Lean universe-level harness under KLEE.
# Prerequisite: lean_klee_repro.sh has produced $ROOT/bc and $ROOT/bc2.
#
#   ./run_harness.sh /scratch/$USER/leanse [max-time-seconds]
set -euo pipefail

ROOT="${1:-/scratch/$USER/leanse}"
MAXTIME="${2:-600}"
P="podman --root $ROOT/proot --runroot $ROOT/prun --cgroup-manager=cgroupfs --events-backend=file"

mkdir -p "$ROOT/harness"; chmod 777 "$ROOT/harness"
cp "$(dirname "$0")/lean_level_harness.cpp" "$ROOT/harness/"

cat > "$ROOT/harness/inner.sh" <<'INNER'
set -eu
cd /w/lean4/src
CXXFLAGS="-std=c++20 -include mutex -emit-llvm -c -I. -Iinclude -I/w/gen -O0 -w"

echo "== prerequisite: symbols the harness needs must be defined in full.bc =="
missing=0
for s in initialize_util_module initialize_kernel_module mk_level_zero mk_univ_param \
         mk_succ mk_max mk_imax normalize is_equivalent is_geq lean_alloc_object; do
  if llvm-nm --defined-only /w/bc2/full.bc 2>/dev/null | grep -q "$s"; then
    echo "  ok      $s"
  else
    echo "  MISSING $s"; missing=$((missing+1))
  fi
done
[ "$missing" -eq 0 ] || echo "WARNING: $missing expected symbols are absent; the link will likely fail."

echo "== compile harness =="
clang++ $CXXFLAGS -DKLEE_MODE -I/tmp/klee_src/include \
        -o /w/harness/harness.bc /w/harness/lean_level_harness.cpp

echo "== link with kernel + runtime =="
llvm-link -o /w/harness/linked.bc /w/harness/harness.bc /w/bc2/full.bc
opt -verify -disable-output /w/harness/linked.bc && echo "verify: OK"
echo "undefined after link: $(llvm-nm --undefined-only /w/harness/linked.bc | wc -l)"

echo "== KLEE =="
rm -rf /w/harness/kout
klee --libc=uclibc --posix-runtime \
     --output-dir=/w/harness/kout \
     --max-time="${MAXTIME}s" \
     --max-memory=8000 \
     --search=random-path --use-batching-search \
     --warnings-only-to-file \
     /w/harness/linked.bc 2>&1 | tail -20

echo "== results =="
ls /w/harness/kout/*.err 2>/dev/null | head -20 || echo "(no error files)"
INNER

$P run --rm --userns=keep-id -e MAXTIME="$MAXTIME" -v "$ROOT":/w:z \
   klee/klee:3.1 bash -lc 'bash /w/harness/inner.sh'
