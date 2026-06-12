# Vacuity in Step-1.5c global-invariant assumes (soundness hole)

**Found 2026-06-12** while validating the string-copy SOURCE fixes on the VibeOS
`vfs.c` module under `--agentic`.

## The bug

A file-scope pointer global with a NULL initializer that is only set by an
`*_init()` function keeps its NULL initializer in the per-function harness (the
init function never runs). The Step-1.5c global-invariant extractor classifies
it `init-trusted` and the **CBMC (static) harness emits**

```c
__CPROVER_assume(mem_root != NULL);   /* mem_root is static ... = NULL */
```

Since `mem_root` is provably NULL at that point, this is
`__CPROVER_assume(NULL != NULL)` = **`__CPROVER_assume(false)`**, which makes the
**entire function verify VACUOUSLY** — every property (OOB, overflow, null-deref,
the function's own asserts) is trivially discharged. CBMC reports
`VERIFICATION SUCCESSFUL` / "1 VCC after simplification" and the pipeline records
a clean function while actually checking **nothing**.

On `vfs.c` this silently masked **13 of 27 functions** (every function that
references `mem_root`), including the real `vfs_open_handle` strcpy overflow.

The **dynamic** harness already fixes its analogue (Bug B, commit 279b486) via
`_emit_dynamic_global_invariant_inits()` — `if (!g) g = calloc(1, sizeof(*g));` —
but the **static/CBMC** harness only emits the assume, never the materialization.

## Confirmed diagnosis

`bmc_agent/bmc_engine.py:_check_function_impl` ran the real `vfs_open_handle`
harness at `--unwind 258` and got `VERIFICATION SUCCESSFUL`. An injected explicit
OOB (`path_copy[300]='A'`) and an injected `__CPROVER_assert(0)` inside the
`if (temp->data)` block were BOTH reported SUCCESS — i.e. the block was
"unreachable", the hallmark of `assume(false)`. Deleting the one
`__CPROVER_assume(mem_root != NULL)` line → `VERIFICATION FAILED` (overflow
caught). Replacing it with `if (!mem_root) mem_root = calloc(1, sizeof(*mem_root));`
+ the assume → also `VERIFICATION FAILED` (caught, non-vacuous).

## The validated fix (NOT committed — needs supervision)

In `bmc_agent/harness_generator.py`, at BOTH static-harness Step-1.5c sites,
materialize init-trusted pointer globals before the assumes:

```python
gi_inits = _emit_dynamic_global_invariant_inits(parsed_file, self.config, None)
gi_assumes = _emit_global_invariant_assumptions(parsed_file, self.config)
if gi_inits or gi_assumes:
    # emit gi_inits (if (!g) g = calloc(1, sizeof(*g));) BEFORE gi_assumes
```

This un-vacuums verification and the overflow is caught at unwind 258.

## Why it was reverted (the caveat to resolve)

Un-vacuuming makes verification REAL for the 13 previously-masked functions, so
they surface counterexamples (the `calloc`-zeroed `mem_root` is an empty/zeroed
tree, not the fully-linked tree real callers pass — functions that walk
`mem_root->children`/`->parent` hit harness-artifact derefs). On the FIXED
`vfs.c` this produced 13 CEXs (vs 0 vacuous before), one CBMC timeout, and a
slower run. Whether they are all filtered to spurious by the agentic realism
gate was not confirmed (run was stopped).

**Open questions for a supervised pass:**
1. Does the realism/classification gate filter the incomplete-tree CEXs to ~0
   confirmed? (Run vfs + a couple other modules to measure the confirmed-FP rate.)
2. Should the materialization model the global as **nondet-but-valid** (so CBMC
   explores realistic trees) rather than a single `calloc`-zeroed object?
3. Cross-target FP-rate of the un-vacuum across non-vibeos projects.

The vacuity bug itself is unambiguous and should be fixed; only the materialization
**model** + its FP impact needs the supervised evaluation above.
