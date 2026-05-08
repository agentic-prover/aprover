# BUG-08 — `memory_heap_start` (memory)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/memory.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires true`

**Postcondition:** `ensures \result == heap_start && \result > 0`

## Counterexample

**Violated property:** `main.assertion.2`

**Key variable assignments:**
```
heap_end = 0ul
heap_start = 0ul
ram_base = 0ul
ram_size = 0ul
result = 0ul
return_value_memory_heap_start = 0ul
goto_symex$$return_value$$memory_heap_start = 0ul
```

## Root cause

CBMC reports a `main.assertion.2` failure — a semantic / contract violation in `memory_heap_start`.

**Realism checker's key concern:** CBMC analyzes the function in isolation without constraining heap_start to be post-initialization non-zero, producing a symbolic witness (heap_start=0) that could only occur in real execution if memory_heap_start() is called before memory_init(). Whether such a call ordering is possible depends on initialization sequencing guarantees not visible in this analysis scope.

**Validator reasoning:** 'memory_heap_start' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`memory_heap_start` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** CBMC analyzes the function in isolation without constraining heap_start to be post-initialization non-zero, producing a symbolic witness (heap_start=0) that could only occur in real execution if memory_heap_start() is called before memory_init(). Whether such a call ordering is possible depends on initialization sequencing guarantees not visible in this analysis scope.

Q1 — Can the violation TYPE occur? The function simply returns the global `heap_start`. The counterexample shows `heap_start = 0ul`, which would be the case if (a) `memory_init()` has not yet been called, or (b) the system is in early boot before heap initialization. In an embedded/OS context, if any code path invokes `memory_heap_start()` before `memory_init()` completes, the function returns 0, violating any assertion that checks the result is a valid non-zero address. This ordering hazard is a realistic class of bug in system initialization code. Q2 — Are the specific witness values achievable? CBMC treats global variables as unconstrained (zero-initialized as a default) when analyzing `memory_heap_start` in isolation without a call to `memory_init()`. The value `heap_start = 0ul` is therefore a CBMC artifact of missing initialization context rather than a proven program path. The dynamic harness also ran to completion without triggering the fault, consistent with the harness having correct initialization ordering. The underlying concern is real — initialization order dependency — but the specific counterexample is produced by CBMC's symbolic treatment of uninitialized globals rather than a demonstrated real execution path.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
