#!/usr/bin/env bash
# Build Lean's runtime and stdlib NATIVELY and replay a solver-generated witness
# against the real implementation, with no KLEE and none of our four models.
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/native.sh" <<'INNER'
set -u
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq libgmp-dev libuv1-dev 2>&1 | tail -1
echo "deps: gmp=$(ls /usr/include/gmp.h 2>/dev/null) uv=$(ls /usr/include/uv.h 2>/dev/null)"

mkdir -p /w/native/obj /w/native/rt
cd /w/lean4

# ---- 1. Lean runtime (C++), natively ----
if [ ! -f /w/native/rt/.done3 ]; then
  cd /w/lean4/src
  find runtime util kernel -name '*.cpp' > /tmp/rtlist.txt
  xargs -P 48 -I{} sh -c 'o="/w/native/rt/$(echo "$1" | tr / _ | sed "s/\.cpp$/.o/")";
      g++ -std=c++20 -include mutex -O1 -w -fPIC -c -I. -Iinclude -I/w/gen -o "$o" "$1" 2>/dev/null' _ {} < /tmp/rtlist.txt
  touch /w/native/rt/.done3
fi
echo "runtime+kernel objects: $(ls /w/native/rt/*.o 2>/dev/null | wc -l) of $(wc -l < /tmp/rtlist.txt)"
echo "kernel level.o present: $(ls /w/native/rt/kernel_level.o 2>/dev/null || echo NO)"

# ---- 2. stage0 stdlib (Lean-generated C), natively ----
cd /w/lean4/stage0/stdlib
find . -name '*.c' | sed 's,^\./,,' > /tmp/stdlist.txt
if [ ! -f /w/native/obj/.done ]; then
  xargs -P 48 -I{} sh -c 'o="/w/native/obj/$(echo "$1" | tr / _ | sed "s/\.c$/.o/")";
      gcc -O1 -w -fPIC -c -I/w/lean4/stage0/src/include -I/w/gen -o "$o" "$1" 2>/dev/null' _ {} < /tmp/stdlist.txt
  touch /w/native/obj/.done
fi
echo "stdlib objects: $(ls /w/native/obj/*.o 2>/dev/null | wc -l) of $(wc -l < /tmp/stdlist.txt)"

cd /w/native
rm -f libstd.a librt.a
ar rcs librt.a rt/*.o 2>/dev/null
# Level.o is replaced per-variant, so keep it out of the archive
rm -f obj/Lean_Level.o
ar rcs libstd.a obj/*.o 2>/dev/null
echo "archives: librt.a=$(stat -c%s librt.a 2>/dev/null) libstd.a=$(stat -c%s libstd.a 2>/dev/null)"

# ---- 3. build the two replay binaries ----
for v in orig mim; do
  [ "$v" = orig ] && SRC=/w/lean4/stage0/stdlib/Lean/Level.c || SRC=/w/mutant/Level_mim.c
  gcc -O1 -w -fPIC -c -I/w/lean4/stage0/src/include -I/w/gen -o /w/native/Level_$v.o "$SRC" || { echo "$v: Level compile FAIL"; continue; }
  gcc -O1 -w -I/w/lean4/stage0/src/include -I/w/gen -o /w/native/replay_$v \
      /w/harness/replay_level.c /w/native/Level_$v.o \
      -Wl,--start-group /w/native/libstd.a /w/native/librt.a -Wl,--end-group \
      -lstdc++ -lgmp -luv -lpthread -ldl -lm 2>/tmp/link_$v.err
  if [ -x /w/native/replay_$v ]; then echo "$v: linked OK"; else echo "$v: LINK FAILED"; head -15 /tmp/link_$v.err; fi
done

# ---- 4. native replay of the solver-generated witnesses ----
cd /w/native
for W in "03000300ffffffff01030003ffffffff" "03000200ffffffff01020002ffffffff --nomax"; do
  echo "=========== NATIVE REPLAY, witness $W"
  for v in orig mim; do
    echo "--- $v ---"
    timeout 60 ./replay_$v $W 2>/dev/null | sed 's/^/    /'; echo "    exit=${PIPESTATUS[0]}"
  done
done
INNER

$P run --rm --user 0 -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc "bash /w/harness/native.sh"
