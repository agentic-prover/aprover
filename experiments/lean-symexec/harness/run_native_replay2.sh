#!/usr/bin/env bash
# Native replay of EXPERIMENT 2 (faithful normalize mimic) under the SEMANTIC oracle.
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/native2.sh" <<'INNER'
set -u
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq libgmp-dev libuv1-dev >/dev/null 2>&1
mkdir -p /w/mutant
python3 /w/mutate_cxx_normalize.py || exit 1

cd /w/lean4/src
# native objects for the two kernel level.cpp variants (real libstdc++, no shims)
g++ -std=c++20 -include mutex -O1 -w -fPIC -c -I. -Iinclude -I/w/gen \
    -o /w/native/lvl_orig.o kernel/level.cpp          2>&1 | grep error: | head -3
g++ -std=c++20 -include mutex -O1 -w -fPIC -c -I. -Iinclude -I/w/gen \
    -o /w/native/lvl_mimic.o /w/mutant/level_norm_mut.cpp 2>&1 | grep error: | head -3
echo "native level objects: $(ls /w/native/lvl_*.o 2>/dev/null | wc -l)/2"

# the Lean-side level constructors (lean_level_mk_succ etc.) live in the
# Lean-generated C, which experiment 1 removed from libstd.a; rebuild that object
gcc -O1 -w -fPIC -c -I/w/lean4/stage0/src/include -I/w/gen \
    -o /w/native/Lean_Level_orig.o /w/lean4/stage0/stdlib/Lean/Level.c 2>&1 | grep error: | head -3
echo "Lean-side Level.o: $(ls /w/native/Lean_Level_orig.o 2>/dev/null | wc -l)/1"

cd /w/native
# librt.a already holds the ORIGINAL kernel_level.o; drop it so our variant wins
cp librt.a librt_nolvl.a 2>/dev/null
ar d librt_nolvl.a rt_kernel_level.o 2>/dev/null
ar d librt_nolvl.a kernel_level.o 2>/dev/null
for v in orig mimic; do
  g++ -std=c++20 -include mutex -O1 -w -I/w/lean4/src -I/w/lean4/src/include -I/w/gen \
      -o rpp_$v /w/harness/replay_param.cpp lvl_$v.o Lean_Level_orig.o \
      -Wl,--start-group libstd.a librt_nolvl.a -Wl,--end-group \
      -lgmp -luv -lpthread -ldl -lm 2>/tmp/lk_$v.err
  if [ -x rpp_$v ]; then echo "$v: linked OK"; else echo "$v: LINK FAILED"; head -8 /tmp/lk_$v.err; fi
done

echo
echo "########## NATIVE REPLAY, experiment 2 witness: lvl=0x36ffffffffff02360103ffff sigma=0x0000"
for v in orig mimic; do
  [ -x rpp_$v ] || continue
  echo "---- $v ----"
  timeout 60 ./rpp_$v 0x36ffffffffff02360103ffff 0x0000 2>&1 | sed 's/^/    /'
  echo "    exit=${PIPESTATUS[0]}"
done
INNER

$P run --rm --user 0 -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/native2.sh'
