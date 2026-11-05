#!/usr/bin/env bash
# Same mutation experiment, now on the ACTUAL C++ kernel under KLEE + libc++.
set -u
R="${1:-/scratch/$USER/lse2}"
T="${2:-600}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/cxxmut.sh" <<'INNER'
set -u
LIBCXX=/tmp/libc++-install-130/include/c++/v1
mkdir -p /w/mutant
python3 /w/mutate_cxx_level.py || exit 1
cd /w/lean4/src
clang++ -std=c++20 -emit-llvm -c -O0 -w -nostdinc++ -I/w/cxxshim -I$LIBCXX \
  -I. -Iinclude -I/w/gen -o /w/bcxx_mut_level.bc /w/mutant/level_mut.cpp 2>&1 | grep error: | head -5
cd /w/harness
clang++ -std=c++20 -emit-llvm -c -O0 -w -DKLEE_MODE -DFOCUS -nostdinc++ \
  -I/w/cxxshim -I$LIBCXX -I/w/lean4/src -I/w/lean4/src/include -I/w/gen -I/tmp/klee_src/include \
  -DRAW_CONSTRUCTORS -o hcxx.bc lean_level_harness.cpp 2>&1 | grep -E "error|fatal" | head -5
cd /w/lean4/src
[ -f /w/bcxx_mut_level.bc ] || { echo "MUTANT COMPILE FAILED"; exit 1; }
echo "mutated kernel/level.cpp compiled"

cd /w/harness
for v in orig mut; do
  if [ "$v" = orig ]; then LVL=/w/bcxx/kernel_level.bc; else LVL=/w/bcxx_mut_level.bc; fi
  OTHER=$(ls /w/bcxx/kernel_*.bc | grep -v kernel_level.bc)
  llvm-link -o M0_$v.bc hcxx.bc "$LVL" $OTHER /w/bcxx/runtime_*.bc /w/bcxx/util_*.bc 2>/dev/null
  cur=M0_$v.bc
  for i in 1 2 3 4 5 6; do
    llvm-link -only-needed -o M${i}_$v.bc "$cur" /w/bcstd/*.bc 2>/dev/null
    u=$(llvm-nm --undefined-only M${i}_$v.bc|wc -l); pv=$(llvm-nm --undefined-only "$cur"|wc -l)
    cur=M${i}_$v.bc; [ "$u" = "$pv" ] && break
  done
  rm -rf CX_$v
  ( klee --libcxx --libc=uclibc --posix-runtime --output-dir=CX_$v --max-time=${MAXT}s \
         --max-memory=8000 --search=dfs --warnings-only-to-file "$cur" > CX_$v.out 2>&1 ) &
done
wait

for v in orig mut; do
  echo "=================== C++ kernel: $v ==================="
  grep -E "KLEE: done: (total instructions|completed paths|generated tests)" CX_$v.out | sed 's/^/  /'
  echo "  CONTRACT VIOLATIONS : $(ls CX_$v/*.contract 2>/dev/null | wc -l)"
  echo "  all error files     : $(ls CX_$v/*.err 2>/dev/null | wc -l)"
  f=$(ls CX_$v/*.contract 2>/dev/null | head -1)
  if [ -n "$f" ]; then
    echo "  --- $f"; head -3 "$f" | sed 's/^/     /'
    k="${f%.contract}.ktest"; [ -e "$k" ] && { echo "  --- WITNESS"; ktest-tool "$k" | grep hex | sed 's/^/     /'; }
  fi
done
INNER

$P run --rm --user 0 -e MAXT="$T" -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/cxxmut.sh'
