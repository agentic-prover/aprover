# CBMC Entry-Oracle — hint-directed entry-point verification with ASan-replay refinement

A way to find **real, reproducible** memory-safety bugs in C parsers by driving
CBMC myself from the **real entry point** (a byte buffer + length), instead of
bmc-agent's per-function harnesses (which invent preconditions → false positives).

**Name:** `cbmc-entry-oracle`  ·  **Lives in:** `tools/cbmc_direct/`

## The idea in one line
Entry-point CBMC ⇒ every explored state is reachable ⇒ no precondition-FP class,
and the counterexample IS concrete input bytes ⇒ replay through the real ASan
build = ground-truth confirmation + PoC.

## The tractability spectrum (pick the smallest scope that covers the target)
CBMC blows up on whole real parsers, so scope down until it completes:
1. **Leaf** — one self-contained `(buf,len)` routine, all else absent. *Validated tractable.*
2. **Stubbed** — entry concrete, heavy callees as **under-approx** stubs (no-op / return-0:
   FP-free, just less coverage). Refine = un-stub a callee for more coverage.
3. **Hint-directed slice (whole program)** — use a bmc-agent `memory_safety` per-function CEx
   as the target: `goto-cc` whole-program-from-entry → instrument the suspect property →
   `goto-instrument --reachability-slice-fb` toward it → `cbmc` the slice. The slice removes
   everything not on a path to the suspect, which is what makes whole-program tractable.

## The validated CBMC config (decisive — heavy flags time out)
Keep: `--bounds-check --pointer-check` (the real memory-safety classes), small `MAXLEN` (4–8),
`--unwind = MAXLEN+2`, `--object-bits 12`.
DROP: `--unwinding-assertions` (loops truncate at bound — fine for bug-FINDING), and the
overflow/conversion checks (they explode the formula and mostly yield arithmetic FPs).

## Components
- `concretize.py` — CBMC `--json-ui --trace` CEx → input file (parses `data[i]`/`size`).
- `cmark_replay_driver.c` + `cmark_replay` (compiled) — ASan/UBSan binary mirroring the harness;
  replays an input file. Crash here = confirmed bug + PoC. Build: `gcc -fsanitize=address,undefined`.
- `run_entry_oracle.sh <target>` — chains harness → cbmc(escalating bounds) → concretize → replay.
- Harnesses (3 spectrum points): `cmark_utf8_leaf.c` (leaf), `cmark_stubbed_harness.c` (stubbed),
  `cmark_entry_harness.c` (whole, intractable — kept as the baseline).

## HOW TO START (directly)
```bash
# Leaf-oracle on a configured target (cmark today):
bash tools/cbmc_direct/run_entry_oracle.sh cmark

# One-off leaf CBMC (light config that completes):
cd /tmp/oss_fuzz_corpora/cmark && \
cbmc tools/cbmc_direct/cmark_utf8_leaf.c src/utf8.c src/buffer.c src/cmark.c src/cmark_ctype.c \
  -I src --function cbmc_entry --unwind 7 --bounds-check --pointer-check -DMAXLEN=6 --object-bits 12

# Hint-directed slice (whole program), per bmc-agent memory_safety CEx target:
#   goto-cc <main-wrapped harness> <srcs> -I src -DMAXLEN=6 -o wp.goto
#   goto-instrument --bounds-check --pointer-check wp.goto chk.goto
#   goto-instrument --reachability-slice-fb chk.goto sliced.goto   # (directed: instrument only the suspect fn)
#   cbmc sliced.goto --unwind 7 --object-bits 12
```

## Status (2026-05-30)
- **Validated:** leaf entry-CBMC completes & verifies (utf8proc_valid: 0/857 props, all ≤6-byte inputs).
- **Wall:** whole-program & stubbed-block-parser time out (cmark scanners unwind to iter 141+).
- **Open empirical question:** does a *directed* slice toward one suspect make whole-program
  tractable? (Slice toward all-assertions keeps 110/210 fns — too much; directed is the lever.)
- **Tooling here:** cbmc/goto-cc/goto-instrument + gcc/ASan only. NO clang/libFuzzer/AFL.

## Next steps to wire it as the 24/7 loop
1. Auto-enumerate self-contained `(buf,len)` leaf routines per project → run leaf-oracle on each.
2. Filter bmc-agent CEx to `memory_safety`; for each, do the **directed** slice (instrument only
   the suspect fn's property) → cbmc → ASan replay. Confirmed crashes = bugs+PoCs; else FP.
3. Per-project config table in `run_entry_oracle.sh` (sources / includes / entry signature).
