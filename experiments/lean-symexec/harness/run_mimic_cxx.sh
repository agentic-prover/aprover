#!/usr/bin/env bash
# FAITHFUL MIMIC experiment on the C++ kernel.
#  - mutation = the Arena's documented MECHANISM (drop the accumulated successor
#    offset in normalize's IMax case), not its conclusion
#  - grammar includes universe parameters, as the Arena's statement requires
set -u
R="${1:-/scratch/$USER/lse2}"
T="${2:-900}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/mimcxx.sh" <<'INNER'
set -u
LIBCXX=/tmp/libc++-install-130/include/c++/v1
mkdir -p /w/mutant
python3 /w/mutate_cxx_normalize.py || exit 1
cd /w/lean4/src
clang++ -std=c++20 -emit-llvm -c -O0 -w -nostdinc++ -I/w/cxxshim -I$LIBCXX \
  -I. -Iinclude -I/w/gen -o /w/bcxx_norm_mut.bc /w/mutant/level_norm_mut.cpp 2>&1 | grep error: | head -5
[ -f /w/bcxx_norm_mut.bc ] || { echo "MIMIC COMPILE FAILED"; exit 1; }
echo "mimic (normalize offset dropped) compiled"

cd /w/harness
for FOC in "" "-DFOCUS"; do
  tag_f=$([ -z "$FOC" ] && echo free || echo focus)
  clang++ -std=c++20 -emit-llvm -c -O0 -w -DKLEE_MODE $FOC -nostdinc++ \
    -I/w/cxxshim -I$LIBCXX -I/w/lean4/src -I/w/lean4/src/include -I/w/gen -I/tmp/klee_src/include \
    -o hp_$tag_f.bc lean_level_param_harness.cpp 2>&1 | grep -E "error|fatal" | head -6
  [ -f hp_$tag_f.bc ] || { echo "HARNESS $tag_f FAILED"; exit 1; }
done
echo "param harnesses compiled"

for tag_f in free focus; do
for v in orig mimic; do
  if [ "$v" = orig ]; then LVL=/w/bcxx/kernel_level.bc; else LVL=/w/bcxx_norm_mut.bc; fi
  OTHER=$(ls /w/bcxx/kernel_*.bc | grep -v kernel_level.bc)
  llvm-link -o P0_${tag_f}_$v.bc hp_$tag_f.bc "$LVL" $OTHER /w/bcxx/runtime_*.bc /w/bcxx/util_*.bc 2>/dev/null
  cur=P0_${tag_f}_$v.bc
  for i in 1 2 3 4 5 6; do
    llvm-link -only-needed -o P${i}_${tag_f}_$v.bc "$cur" /w/bcstd/*.bc 2>/dev/null
    u=$(llvm-nm --undefined-only P${i}_${tag_f}_$v.bc|wc -l); pv=$(llvm-nm --undefined-only "$cur"|wc -l)
    cur=P${i}_${tag_f}_$v.bc; [ "$u" = "$pv" ] && break
  done
  rm -rf MC_${tag_f}_$v
  ( klee --libcxx --libc=uclibc --posix-runtime --output-dir=MC_${tag_f}_$v --max-time=${MAXT}s \
         --max-memory=8000 --search=dfs --warnings-only-to-file "$cur" > MC_${tag_f}_$v.out 2>&1 ) &
done
done
wait

for tag_f in free focus; do
for v in orig mimic; do
  echo "=========== $tag_f / $v ==========="
  grep -E "KLEE: done: (total instructions|completed paths|partially)" MC_${tag_f}_$v.out | sed 's/^/  /'
  echo "  CONTRACT VIOLATIONS : $(ls MC_${tag_f}_$v/*.contract 2>/dev/null | wc -l)"
  echo "  other errors        : $(ls MC_${tag_f}_$v/*.err 2>/dev/null | wc -l)"
  f=$(ls MC_${tag_f}_$v/*.contract 2>/dev/null | head -1)
  if [ -n "$f" ]; then
    echo "  --- $f"; head -2 "$f" | sed 's/^/     /'
    k="${f%.contract}.ktest"; [ -e "$k" ] && { echo "  --- WITNESS"; ktest-tool "$k" | grep hex | sed 's/^/     /'; }
  fi
done
done
INNER

$P run --rm --user 0 -e MAXT="$T" -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/mimcxx.sh'
