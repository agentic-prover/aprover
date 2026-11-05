#!/usr/bin/env bash
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"
cat > "$R/harness/trace.sh" <<'INNER'
set -u
cd /w/harness
I="-I/w/lean4/stage0/src/include -I/w/gen -I/tmp/klee_src/include"
clang -emit-llvm -c -O0 -w -DKLEE_MODE -DFOCUS -DNOMAX -DTRACE $I -o g_tr.bc lean_level_c_harness.c || exit 1
for v in orig mim; do
  [ "$v" = orig ] && L=/w/bc3/Level.bc || L=/w/mutant/Level_mim.bc
  llvm-link -o /tmp/t0.bc g_tr.bc "$L" 2>/dev/null
  cur=/tmp/t0.bc
  for i in 1 2 3 4 5; do
    llvm-link -only-needed -o /tmp/t$i.bc "$cur" /w/bcstd/*.bc 2>/dev/null
    u=$(llvm-nm --undefined-only /tmp/t$i.bc|wc -l); pv=$(llvm-nm --undefined-only "$cur"|wc -l)
    cur=/tmp/t$i.bc; [ "$u" = "$pv" ] && break
  done
  rm -rf W_$v
  timeout 500 klee --libc=uclibc --output-dir=W_$v --max-time=420s --search=dfs "$cur" > W_$v.out 2>&1
  echo "=========== $v ==========="
  grep -oE "TRACE [a-zA-Z0-9(),]+.*" W_$v/warnings.txt 2>/dev/null | sed 's/ *$//' | sort | uniq -c | sed 's/^/  /'
  echo "  contract: $(ls W_$v/*.contract.err 2>/dev/null|wc -l)  paths: $(grep -oE 'completed paths = [0-9]+' W_$v.out|tail -1)"
done
INNER
$P run --rm --userns=keep-id -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/trace.sh'
