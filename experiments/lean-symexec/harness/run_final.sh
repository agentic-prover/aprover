#!/usr/bin/env bash
# Eight arms: {full grammar, no-max grammar} x {free shape, family focus} x {orig, mimic}
set -u
R="${1:-/scratch/$USER/lse2}"
T="${2:-1800}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

cat > "$R/harness/final.sh" <<'INNER'
set -u
cd /w/harness
I="-I/w/lean4/stage0/src/include -I/w/gen -I/tmp/klee_src/include"
clang -emit-llvm -c -O0 -w -DKLEE_MODE          $I -o g_free.bc    lean_level_c_harness.c || exit 1
clang -emit-llvm -c -O0 -w -DKLEE_MODE -DFOCUS  $I -o g_focus.bc   lean_level_c_harness.c || exit 1
clang -emit-llvm -c -O0 -w -DKLEE_MODE -DNOMAX  $I -o g_nmfree.bc  lean_level_c_harness.c || exit 1
clang -emit-llvm -c -O0 -w -DKLEE_MODE -DFOCUS -DNOMAX $I -o g_nmfocus.bc lean_level_c_harness.c || exit 1
clang -emit-llvm -c -O0 -w -I/w/lean4/stage0/src/include -I/w/gen \
      -o /w/mutant/Level_mim.bc /w/mutant/Level_mim.c || exit 1
echo "compiled"

link() {
  llvm-link -o /tmp/$3_0.bc "$1" "$2" 2>/dev/null
  cur=/tmp/$3_0.bc
  for i in 1 2 3 4 5; do
    llvm-link -only-needed -o /tmp/$3_$i.bc "$cur" /w/bcstd/*.bc 2>/dev/null
    u=$(llvm-nm --undefined-only /tmp/$3_$i.bc|wc -l); pv=$(llvm-nm --undefined-only "$cur"|wc -l)
    cur=/tmp/$3_$i.bc; [ "$u" = "$pv" ] && break
  done
  cp "$cur" "E_$3.bc"
}
O=/w/bc3/Level.bc; M=/w/mutant/Level_mim.bc
link g_free.bc    $O free_orig   ; link g_free.bc    $M free_mim
link g_focus.bc   $O focus_orig  ; link g_focus.bc   $M focus_mim
link g_nmfree.bc  $O nmfree_orig ; link g_nmfree.bc  $M nmfree_mim
link g_nmfocus.bc $O nmfocus_orig; link g_nmfocus.bc $M nmfocus_mim
echo "linked $(ls E_*.bc|wc -l)"

ARMS="free_orig free_mim focus_orig focus_mim nmfree_orig nmfree_mim nmfocus_orig nmfocus_mim"
for tag in $ARMS; do
  rm -rf V_$tag
  ( klee --libc=uclibc --output-dir=V_$tag --max-time=${MAXT}s --max-memory=8000 \
         --search=dfs --warnings-only-to-file E_$tag.bc > V_$tag.out 2>&1 ) &
done
wait

for tag in $ARMS; do
  echo "=================== $tag ==================="
  grep -E "KLEE: done: (total instructions|completed paths|generated tests)" V_$tag.out | sed 's/^/  /'
  echo "  CONTRACT VIOLATIONS : $(ls V_$tag/*.contract 2>/dev/null | wc -l)"
  echo "  other errors        : $(ls V_$tag/*.err 2>/dev/null|wc -l)"
  f=$(ls V_$tag/*.contract 2>/dev/null | head -1)
  if [ -n "$f" ]; then
    echo "  --- $f ---"; head -4 "$f" | sed 's/^/    /'
    k="${f%.contract}.ktest"
    [ -e "$k" ] && { echo "  --- WITNESS ---"; ktest-tool "$k" | sed 's/^/    /'; }
  fi
done
INNER

$P run --rm --userns=keep-id -e MAXT="$T" -v "$R":/w:z docker.io/klee/klee:3.1 \
   bash -lc 'bash /w/harness/final.sh'
