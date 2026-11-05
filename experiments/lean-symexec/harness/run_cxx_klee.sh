#!/usr/bin/env bash
# Symbolically execute the C++ kernel itself, using KLEE's bitcode libc++.
set -u
R="${1:-/scratch/$USER/lse2}"
T="${2:-600}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/cxxklee.sh" <<'INNER'
set -u
LIBCXX=/tmp/libc++-install-130/include/c++/v1
cd /w/harness
clang++ -std=c++20 -emit-llvm -c -O0 -w -DKLEE_MODE -nostdinc++ \
  -I/w/cxxshim -I$LIBCXX -I/w/lean4/src -I/w/lean4/src/include -I/w/gen -I/tmp/klee_src/include \
  -o hcxx.bc lean_level_harness.cpp 2>&1 | grep -E "error|fatal" | head -8
[ -f hcxx.bc ] || { echo "HARNESS COMPILE FAILED"; exit 1; }
echo "harness compiled"

llvm-link -o C0.bc hcxx.bc /w/bcxx/kernel_*.bc /w/bcxx/runtime_*.bc /w/bcxx/util_*.bc 2>/dev/null
echo "linked C++ side: $(llvm-nm --undefined-only C0.bc | wc -l) undefined"
cur=C0.bc
for i in 1 2 3 4 5 6; do
  llvm-link -only-needed -o C$i.bc "$cur" /w/bcstd/*.bc 2>/dev/null
  u=$(llvm-nm --undefined-only C$i.bc | wc -l); pv=$(llvm-nm --undefined-only "$cur" | wc -l)
  cur=C$i.bc; [ "$u" = "$pv" ] && break
done
echo "after stdlib closure: $(llvm-nm --undefined-only $cur | wc -l) undefined ($cur)"
echo "--- remaining undefined (first 25) ---"
llvm-nm --undefined-only $cur | sed 's/^/  /' | head -25

rm -rf CXX_OUT
klee --libcxx --libc=uclibc --posix-runtime --output-dir=CXX_OUT \
     --max-time=${MAXT}s --max-memory=8000 --search=dfs \
     --warnings-only-to-file "$cur" 2>&1 | grep -E "KLEE: (done|ERROR|WARNING: undefined)" | head -20
echo "  contract violations : $(ls CXX_OUT/*.contract 2>/dev/null | wc -l)"
echo "  all error files     : $(ls CXX_OUT/*.err 2>/dev/null | wc -l)"
for f in CXX_OUT/*.err; do [ -e "$f" ] && { echo "  --- $f"; head -6 "$f" | sed 's/^/     /'; break; }; done
INNER

$P run --rm --user 0 -e MAXT="$T" -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/cxxklee.sh'
