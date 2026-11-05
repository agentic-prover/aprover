# Symbolic execution of the Lean kernel's universe-level algebra

Runs KLEE over Lean 4's **official C++ kernel**, compiled to LLVM bitcode against
KLEE's bitcode libc++, and over the Lean-generated C in `stage0/stdlib`. Every
witness replays natively against the real runtime and kernel.

Target: `leanprover/lean4` at `82728c38926518a70a2142a59c68146f4615391e`.
Engine: KLEE 3.1 (clang/LLVM 13.0.1), Linux x86_64, inside `docker.io/klee/klee:3.1`.

## Results

**1. The C++ kernel executes symbolically.** 48 of 61 C++ translation units compile
to bitcode, including all 15 kernel units and `runtime/object.cpp`. The 13 that do
not are the libuv, OpenSSL and mimalloc boundary, none in the checking path.
First unfocused run: 34,391 completed paths, 389,324,581 instructions, no errors.

**2. The construction API decides what is reachable.** Injecting an incorrect
`imax`/`succ` equivalence into `is_equivalent`, four configurations run to exhaustion:

| Harness builds through | Kernel | Completed paths | Violations |
| --- | --- | --- | --- |
| `mk_imax` (smart) | unmodified | 2,806 | 0 |
| `mk_imax` (smart) | **faulty** | 2,806 | **0** |
| `mk_imax_core` (raw) | unmodified | 2,806 | 0 |
| `mk_imax_core` (raw) | **faulty** | 2,802 | **1** |

Row two is the point. `mk_imax` rewrites `imax 0 v` to `v` at `level.cpp:110`
before the node exists, so the shape the fault needs cannot be represented. The
search is exhausted, not budget-limited, and the fault is still invisible. A
harness that builds through an API which normalizes on the way in forecloses a
whole family of faults, and the run looks clean while doing it.

**3. A mimic of a documented mechanism, found by an independent oracle.** The Lean
Kernel Arena's `level-imax-normalization` states its mechanism as dropping an
accumulated successor offset when decomposing `imax u (param v)`. `normalize`
restores that offset in its `IMax` case; the mimic deletes the restoration and
changes nothing else. No equivalence is asserted anywhere — the wrong one emerges.

The oracle is a reference semantics (`eval_level`) evaluating levels under a
symbolic assignment of the universe parameters, written independently of
`normalize`. This matters: see docs/PROTOCOL.md §3.

| Grammar (with parameters) | Kernel | Completed paths | Violations |
| --- | --- | --- | --- |
| free | unmodified | 44,318 | 0 |
| free | **mimicked** | 42,644 | **1** |

Witness: `a = u`, `b = max(u, succ (imax 0 0))` at `u = 0`, denoting 0 and 1 while
the kernel's equivalence routine accepts them. Not the pair the Arena displays.

**4. Both witnesses replay natively**, each under the oracle it was found with,
against a native build of 59 of 61 C++ units plus 2,628 of 2,629 stdlib C units,
with the real libstdc++ and none of the replacement headers or models.

| Experiment | Build | Result | Exit |
| --- | --- | --- | --- |
| 1, injected equivalence | unmodified / faulty | holds / **violated** | 0 / 1 |
| 2, normalization mimic | unmodified / mimicked | `eval` 0 vs 1, `is_equivalent` false / **true** | 0 / 1 |

Injected faults are controls, not discoveries. No previously unknown fault is claimed.

## Layout

```
harness/
  cxxshim/                      <atomic> <mutex> <bit> for KLEE's thread-free libc++
  build_cxx_libcxx.sh           C++ kernel + runtime -> bitcode
  build_stdlib_bc.sh            stage0 stdlib C -> bitcode (2,629 files, resumable)
  lean_level_param_harness.cpp  symbolic harness, 5-way grammar with universe params
  lean_level_harness.cpp        earlier C++ harness (smart vs raw constructors)
  lean_level_c_harness.c        harness over the Lean-generated C
  mutate_cxx_normalize.py       the mimic (drops the successor offset)
  mutate_cxx_level.py           the direct injection (asserts the equivalence)
  probe_min.c, run_probe.sh     staged probe: one operation per step
  replay_param.cpp              native replay, semantic oracle
  replay_level.c                native replay, order oracle
  run_*.sh                      drivers
  decode_*witness.py            witness bytes -> level terms
results/                        raw logs behind every number above
docs/PROTOCOL.md                validation rules
```

## Reproducing

```
harness/build_stdlib_bc.sh  <root>        # stage0 stdlib -> bitcode
harness/build_cxx_libcxx.sh <root>        # C++ kernel -> bitcode (needs cxxshim)
harness/run_cxx_mutation.sh <root> 900    # result 2
harness/run_mimic_cxx.sh    <root> 900    # result 3
harness/run_native_replay2.sh <root>      # result 4
harness/decode_param_witness.py 0x36ffffffffff02360103ffff 0x0000
```

## Traps

- KLEE writes `klee_report_error(..., "contract")` as `testNNN.contract`, **not**
  `testNNN.contract.err`. A wrong glob made four successful runs look like misses.
- `--search=bfs` completes zero paths here. Use `dfs`.
- The klee image pins `USER klee`; `--user 0` is needed for `apt`.
- Do **not** call `lean_initialize_runtime_module()` in a batch replay binary —
  it starts libuv's event loop and never returns. Guarded behind `-DINIT_RT`.
- `pkill -f klee` over SSH matches its own command line. Use `pkill -x klee`.
