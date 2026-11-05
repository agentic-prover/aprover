#!/usr/bin/env bash
# Compile the stage0 Lean stdlib (pre-generated C) to LLVM bitcode.
# Resumable: only files without a .bc are compiled.
set -u
R="${1:-/scratch/$USER/lse2}"
P="podman --root $R/proot --runroot $R/prun --cgroup-manager=cgroupfs --events-backend=file"

mkdir -p "$R/bcstd"; chmod 777 "$R/bcstd"

cat > "$R/bcstd/inner.sh" <<'INNER'
set -u
cd /w/lean4/stage0/stdlib
find . -name '*.c' | sed 's,^\./,,' > /w/bcstd/list.txt
: > /w/bcstd/todo.txt
while read -r f; do
  o="/w/bcstd/$(echo "$f" | tr / _ | sed 's/\.c$/.bc/')"
  [ -f "$o" ] || printf '%s\n' "$f" >> /w/bcstd/todo.txt
done < /w/bcstd/list.txt
echo "TOTAL=$(wc -l < /w/bcstd/list.txt) TODO=$(wc -l < /w/bcstd/todo.txt)"

xargs -P 48 -I{} sh -c 'o="/w/bcstd/$(echo "$1" | tr / _ | sed "s/\.c$/.bc/")"; clang -emit-llvm -c -O0 -w -I /w/lean4/stage0/src/include -I /w/gen -o "$o" "$1" 2>/dev/null' _ {} < /w/bcstd/todo.txt

echo "DONE_COMPILED=$(ls /w/bcstd/*.bc 2>/dev/null | wc -l)"
INNER

$P run --rm --userns=keep-id -v "$R":/w:z docker.io/klee/klee:3.1 bash -lc 'bash /w/bcstd/inner.sh'
