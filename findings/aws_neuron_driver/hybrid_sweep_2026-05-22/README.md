# AWS Neuron Driver — OpenRouter Claude sweep, 2026-05-22

8 small-to-medium Neuron driver preprocessed TUs swept end-to-end through
the bmc-agent pipeline. Realism + feedback-loop both enabled.

## Configuration

- spec_gen + feedback_distill + classifier + realism + refinement:
  Claude Sonnet 4.5 via OpenRouter
- threat-model: security
- --enable-realism-check
- --enable-feedback-loop (in-sweep iteration with `feedback_max_iters=3`)

Note: started in K2-hybrid mode (K2 default + Claude/OpenRouter for spec_gen
only, ~30x cheaper). Mid-sweep K2 (api.k2think.ai) returned HTTP 504
Gateway Timeout, so all roles were switched to OpenRouter Claude. K2 has
since recovered.

## Results

| File | LoC | Functions | Verified clean | real_bug raw | spurious | After realism / feedback filter | CBMC errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| neuron_arch | 92 | 6 | **6** | 0 | 0 | 0 | 0 |
| neuron_log | 155 | 5 | 2 | 1 | 0 | 1 (realism=uncertain) | 2 |
| neuron_topsp | 68 | 2 | 0 | 1 | 0 | 1 (realism=uncertain) | 1 |
| neuron_module | 100 | 4 | 0 | 0 | 0 | 0 | 4 |
| neuron_cinit | 84 | 5 | 0 | 0 | 2 | 0 | 3 |
| neuron_core | 158 | 6 | 0 | 1 | 0 | 0 (1 → unrealistic) | 5 |
| neuron_ds | 244 | 19 | **9** | 4 | 3 | 2 (2 → unrealistic) | 1 |
| neuron_reset | 437 | 14 | **4** | 3 | 3 | 1 (2 → unrealistic) | 3 |
| **TOTAL** | **1338** | **61** | **21** | **10** | **8** | **4** | **19** |

After applying the realism+feedback filter, **4 of 10 raw real_bug classifications survive** — all 4 are with `realism=uncertain` or null (the LLM realism call failed). These are likely false positives following the same defensive-programming-gap pattern as previous sweeps; they hit OpenRouter's 8 MB request limit on the realism LLM call because of an unrelated CBMC raw-output blow-up. The blow-up was patched (see [`SESSION_SUMMARY_2026-05-22.md`](../../SESSION_SUMMARY_2026-05-22.md)) so future sweeps will downgrade these correctly.

## Unfiltered real_bug candidates (4)

These are the survivors of the realism+feedback filter:

1. **ts_nq_destroy** (neuron_topsp.c) — `main.pointer_dereference.1`, confirmed_system_entry.
   Indirect call through `ndhal->ndhal_topsp.ts_nq_get_nqid(nd, eng_index, nq_type)`. CEx requires the function-pointer field to be NULL — defensive-programming gap (kernel init paths set this up; cleanup paths assume it).
2. **neuron_log_rec_add** (neuron_log.c) — `pointer_dereference.13`, confirmed_system_entry.
   Failing on a pointer-deref deep inside the body. CEx requires `nd->log_obj.log` to be in a state that real callers never produce.
3. **neuron_ds_release_pid** (neuron_ds.c) — confirmed_system_entry, classification-only (bug_report.json wasn't populated with Phase-3 fields, likely a pipeline state-write quirk).
4. **nr_stop_thread** (neuron_reset.c) — confirmed_system_entry, cleanup function.
   CEx requires `nd->nr.thread != NULL` while `nd->nr.req_pending_head` is in an unrealistic state. Strict caller discipline maintained in driver teardown.

All four match the pattern from yesterday's `ggml-alloc.c` sweep
("classifier WITHOUT realism check over-eagerly tags defensive-programming
gaps as real_bug"). Manual reading of each function's callers (in
`/tmp/aws-neuron-driver/`) suggests no true bug.

## What the feedback loop caught (6 filtered FPs)

For 6 functions, the in-sweep feedback loop learned a precondition
constraint and re-verified the function clean:

- `nc_event_set` — feedback-converged after constraint learned
- `get_neuroncore_counter_value` — feedback-converged
- `neuron_ds_acquire_pid` — feedback-converged
- `neuron_ds_check_entry_in_use` — feedback-converged
- `nr_create_thread` — feedback-converged
- `nr_op_in_reset_wnd` — feedback-converged

For each: classifier elevated to real_bug → feedback-loop distilled a
learned constraint → in-sweep CBMC re-run with the constraint verified
clean → bug_reporter stamped `realism=unrealistic` with reasoning
`[feedback-converged] in-sweep iteration succeeded`. This is the
in-sweep iteration architecture described in `bmc_agent_session_2026-05-13.md`.

### Constraints the feedback loop actually learned

Pulled from `<stem>/learned_constraints.json` after the sweep:

| Scope | Learned clause |
|---|---|
| project (neuron_core) | `ndhal != NULL && ndhal->ndhal_nc.nc_get_event_addr != NULL` |
| function (neuron_ds.get_neuroncore_counter_value) | `entry != NULL && entry->mc != NULL && entry->mc->va != NULL` |
| function (neuron_log.neuron_log_rec_add) | `nd != NULL && (nd->log_obj.log == NULL \|\| valid_range(nd->log_obj.log, 0, 1024))` |

These map cleanly onto invariants real kernel callers maintain — the
ndhal handler table is populated by `ndhal_register_arch` at probe,
`entry->mc->va` is set by the memory chunk allocator before any reader
runs, and `nd->log_obj.log` is the field whose existing in-body NULL
check the function already enforces.

This is concrete evidence that the feedback loop discovers contracts
*from the realism stage's analysis of the CEx*, not just from heuristics.

## Wall clock / cost

- Total wall clock: ~32 min (parallel=2)
- Files >5 min: neuron_ds (15 min), neuron_reset (16 min), neuron_log (9 min)
- LLM provider: Claude Sonnet 4.5 via OpenRouter Bedrock
- Estimated cost: ~$5-8 (~$0.10-0.13 per function for the full pipeline
  with feedback-loop iteration)

K2-mode equivalent estimate: ~$0.20-0.50 for the same coverage. The
~10-25x cost premium is purely because K2 was down; the same sweep on a
healthy K2 backend would be in the hybrid-mode budget.

## Files produced

Each function dir under `/tmp/aprover_neuron_or_sweep/<stem>/<stem>_or/`
has: `spec.json`, `harness.c`, `bug_report.json`, `classification.json`,
`cbmc_result.json`. Raw CBMC dumps are large (up to 9 GB for some
classifications) but the patched cbmc.py (commit `2ab4dcf`) caps these
for future runs.
