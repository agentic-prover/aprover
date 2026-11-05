#!/usr/bin/env bash
set -u
R="${1:-/scratch/$USER/lse2}"
T="${2:-1200}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/mim4.sh" <<'INNER'
set -u
cd /w/harness
clang -emit-llvm -c -O0 -w -DKLEE_MODE -I/w/lean4/stage0/src/include -I/w/gen \
      -I/tmp/klee_src/include -o h_free.bc  lean_level_c_harness.c || exit 1
clang -emit-llvm -c -O0 -w -DKLEE_MODE -DFOCUS -I/w/lean4/stage0/src/include -I/w/gen \
      -I/tmp/klee_src/include -o h_focus.bc lean_level_c_harness.c || exit 1
clang -emit-llvm -c -O0 -w -I/w/lean4/stage0/src/include -I/w/gen \
      -o /w/mutant/Level_mim.bc /w/mutant/Level_mim.c || exit 1

link() {   # $1=harness bc  $2=level bc  $3=out
  llvm-link -o /tmp/l0.bc "$1" "$2" 2>/dev/null
  cur=/tmp/l0.bc
  for i in 1 2 3 4 5; do
    llvm-link -only-needed -o /tmp/l$i.bc "$cur" /w/bcstd/*.bc 2>/dev/null
    u=$(llvm-nm --undefined-only /tmp/l$i.bc | wc -l)
    pv=$(llvm-nm --undefined-only "$cur" | wc -l)
    cur=/tmp/l$i.bc
    [ "$u" = "$pv" ] && break
  done
  cp "$cur" "$3"
}
link h_free.bc  /w/bc3/Level.bc            D_free_orig.bc
link h_free.bc  /w/mutant/Level_mim.bc     D_free_mim.bc
link h_focus.bc /w/bc3/Level.bc            D_focus_orig.bc
link h_focus.bc /w/mutant/Level_mim.bc     D_focus_mim.bc
echo "linked: $(ls -la D_*.bc | wc -l) modules"

for tag in free_orig free_mim focus_orig focus_mim; do
  rm -rf T_$tag
  ( klee --libc=uclibc --posix-runtime --output-dir=T_$tag --max-time=${MAXT}s \
         --max-memory=8000 --search=dfs --warnings-only-to-file D_$tag.bc \
         > T_$tag.out 2>&1 ) &
done
wait

for tag in free_orig free_mim focus_orig focus_mim; do
  echo "=================== $tag ==================="
  grep -E "KLEE: done: (total instructions|completed paths|generated tests)" T_$tag.out | sed 's/^/  /'
  echo "  contract violations : $(ls T_$tag/*.contract.err 2>/dev/null | wc -l)"
  echo "  all error files     : $(ls T_$tag/*.err 2>/dev/null | wc -l)"
  f=$(ls T_$tag/*.contract.err 2>/dev/null | head -1)
  if [ -n "$f" ]; then
    echo "  --- $f ---"; head -6 "$f" | sed 's/^/    /'
    k="$(echo "$f" | sed 's/\..*$//').ktest"
    [ -e "$k" ] && { echo "  --- witness ---"; ktest-tool "$k" | sed 's/^/    /'; }
  fi
done
INNER

$P run --rm --userns=keep-id -e MAXT="$T" -v "$R":/w:z docker.io/klee/klee:3.1 \
   bash -lc 'bash /w/harness/mim4.sh'
