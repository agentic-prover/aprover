#!/usr/bin/env bash
# Compile Lean's C++ kernel, runtime and util to LLVM bitcode against KLEE's
# bitcode libc++, using the single-threaded <atomic> and <mutex> shims.
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/bcxx.sh" <<'INNER'
set -u
LIBCXX=/tmp/libc++-install-130/include/c++/v1
mkdir -p /w/bcxx
cd /w/lean4/src
find kernel runtime util -name '*.cpp' | sort > /tmp/cxxlist.txt
: > /tmp/cxxfail.txt
xargs -P 48 -I{} sh -c '
  o="/w/bcxx/$(echo "$1" | tr / _ | sed "s/\.cpp$/.bc/")"
  if ! clang++ -std=c++20 -emit-llvm -c -O0 -w -nostdinc++ \
       -I/w/cxxshim -I'"$LIBCXX"' -I. -Iinclude -I/w/gen -o "$o" "$1" 2>/dev/null; then
    printf "%s\n" "$1" >> /tmp/cxxfail.txt
  fi' _ {} < /tmp/cxxlist.txt

echo "TOTAL   = $(wc -l < /tmp/cxxlist.txt)"
echo "BUILT   = $(ls /w/bcxx/*.bc 2>/dev/null | wc -l)"
echo "FAILED  = $(wc -l < /tmp/cxxfail.txt)"
echo "--- failures ---"; sort /tmp/cxxfail.txt | sed 's/^/  /'
echo "--- first error of each failure ---"
while read -r f; do
  echo "### $f"
  clang++ -std=c++20 -emit-llvm -c -O0 -w -nostdinc++ -I/w/cxxshim -I$LIBCXX -I. -Iinclude -I/w/gen \
      -o /dev/null "$f" 2>&1 | grep "error:" | head -2 | sed 's/^/    /'
done < /tmp/cxxfail.txt
INNER

$P run --rm --user 0 -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/bcxx.sh'
