#!/usr/bin/env bash
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"
cat > "$R/harness/probe6.sh" <<'INNER'
set -u
cd /w/harness
clang -emit-llvm -c -O0 -w -DKLEE_MODE -DSTEP=6 -I/w/lean4/stage0/src/include \
      -I/w/gen -I/tmp/klee_src/include -o p6.bc probe_min.c || exit 1
clang -emit-llvm -c -O0 -w -I/w/lean4/stage0/src/include -I/w/gen \
      -o /w/mutant/Level_mim.bc /w/mutant/Level_mim.c || exit 1
for v in orig mim; do
  [ "$v" = orig ] && L=/w/bc3/Level.bc || L=/w/mutant/Level_mim.bc
  llvm-link -o /tmp/q0.bc p6.bc "$L" 2>/dev/null
  cur=/tmp/q0.bc
  for i in 1 2 3 4 5; do
    llvm-link -only-needed -o /tmp/q$i.bc "$cur" /w/bcstd/*.bc 2>/dev/null
    u=$(llvm-nm --undefined-only /tmp/q$i.bc|wc -l); pv=$(llvm-nm --undefined-only "$cur"|wc -l)
    cur=/tmp/q$i.bc; [ "$u" = "$pv" ] && break
  done
  rm -rf Q_$v
  timeout 240 klee --libc=uclibc --output-dir=Q_$v --max-time=180s --search=dfs "$cur" > Q_$v.out 2>&1
  echo "=========== $v ==========="
  grep -oE "W: [^\"]*|REACHED END" Q_$v/warnings.txt 2>/dev/null | sort -u | sed 's/^/  /'
  echo "  errors: $(ls Q_$v/*.err 2>/dev/null|wc -l)"
  for f in Q_$v/*.err; do [ -e "$f" ] && { echo "  --- $f"; head -3 "$f"|sed 's/^/    /'; }; done
done
INNER
$P run --rm --userns=keep-id -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/harness/probe6.sh'
