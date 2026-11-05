# Validation protocol

Rules this harness follows. They exist because a symbolic engine will happily
report things that are artefacts of the harness rather than faults in the target.

## 1. A violation is a lead until it replays

Nothing is called a finding until its witness is reconstructed and run against a
**native build of the unmodified target**, outside the symbolic engine, and
produces the same verdict. A result that only exists inside KLEE is not evidence.

## 2. Every external-call model is recorded

Where the engine cannot execute something, we substitute a model, and each model
is written down with its justification and its limits. The models used here:

| Model | Why | Limit |
| --- | --- | --- |
| `lean_level_eq` | transcribed from `operator==` in `src/kernel/level.cpp` | none for these properties |
| `lean_dec_ref_cold` | C++ runtime only; calling it natively corrupts interpreter memory | **hides use-after-free and refcount faults**; no memory-safety claim may rest on it |
| `lean_array_push`, `lean_alloc_object` | C++ runtime only | always-copy push is observationally equal |
| `<atomic>`, `<mutex>`, `<bit>` | KLEE's libc++ is built `_LIBCPP_HAS_NO_THREADS` | exact for a single-threaded interpreter |

## 3. The oracle must not share code with the fault

A property built from a function cannot detect a fault inside that function.
`is_geq` normalizes both arguments before comparing them, so a property phrased
in terms of `is_geq` is blind to a fault in `normalize` — we watched one hold on
a kernel we knew to be broken. Use a reference semantics written independently
of the code under test.

## 4. Harness faults look exactly like target faults

Three faults in this harness presented as target problems before being traced:

1. Modelling `lean_level_eq` via Lean's `Level.beq` — but `l_Lean_Level_beq___boxed`
   is itself a wrapper around `lean_level_eq`, so the two recursed without end and
   every state died on KLEE's stack limit. It looked like a search-strategy problem.
2. `lean_dec_ref_cold` called natively on interpreter-owned memory, corrupting the
   heap and making `l_Lean_Level_geq` diverge.
3. `lean_array_push` / `lean_alloc_object`, same class.

`probe_min.c` with `run_probe.sh` isolates these: it executes one kernel operation
per step and stops at the first that fails to terminate. Run it whenever a new
operation is added.

## 5. Injected faults are controls, never discoveries

A fault we introduced is reported as a control. A *mutation* asserts a wrong
answer directly; a *mimic* implements a documented mechanism and lets the wrong
answer emerge. The two are not equivalent evidence and are labelled separately.
