#!/usr/bin/env bash
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"
cat > "$R/harness/probe.sh" <<'INNER'
set -u
cd /w/harness
for st in 1 2 3 4 5; do
  clang -emit-llvm -c -O0 -w -DKLEE_MODE -DSTEP=$st -I/w/lean4/stage0/src/include \
        -I/w/gen -I/tmp/klee_src/include -o p$st.bc probe_min.c 2>/dev/null || { echo "STEP$st compile FAIL"; continue; }
  llvm-link -o /tmp/p0.bc p$st.bc /w/bc3/Level.bc 2>/dev/null
  cur=/tmp/p0.bc
  for i in 1 2 3 4 5; do
    llvm-link -only-needed -o /tmp/p$i.bc "$cur" /w/bcstd/*.bc 2>/dev/null
    u=$(llvm-nm --undefined-only /tmp/p$i.bc|wc -l); pv=$(llvm-nm --undefined-only "$cur"|wc -l)
    cur=/tmp/p$i.bc; [ "$u" = "$pv" ] && break
  done
  rm -rf P_$st
  timeout 180 klee --libc=uclibc --output-dir=P_$st --max-time=120s --search=dfs "$cur" > P_$st.out 2>&1
  echo "STEP$st: $(grep -c 'REACHED END' P_$st/warnings.txt 2>/dev/null) end | last: $(grep -oE 'STEP[0-9] [a-z_]+ ok|REACHED END' P_$st/warnings.txt 2>/dev/null | tail -1) | $(grep -E 'KLEE: done: (completed paths|total instr)' P_$st.out | tr '\n' ' ')"
done
INNER
$P run --rm --userns=keep-id -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/probe.sh'
