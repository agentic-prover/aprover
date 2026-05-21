# Full bmc-agent LLM pipeline demo on neuron_pid.c

One-shot demonstration of running the FULL bmc-agent pipeline
(Phase 1 LLM spec generation + Phase 2 CBMC + Phase 3 refinement +
realism check) on the same Neuron driver file that the trivial-spec
sweep covered earlier.

**Config:** `BMC_AGENT_LLM_MODEL=claude-sonnet-4-6`, with all the
session's M1/M1.2/M2/kernel-stubs infrastructure enabled.

**Estimated API cost:** ~$1.50 (Anthropic).

## Comparison vs trivial-spec sweep

| Function | Trivial-spec | LLM-spec |
|---|---|---|
| `npid_attach` | VERIFIED | FAIL (npid_attach.unwind.4) |
| `npid_attached_process_count` | VERIFIED | varies |
| `npid_detach` | VERIFIED | varies |
| `npid_find_process_slot` | VERIFIED | varies |
| `npid_find_process_slot_by_task` | VERIFIED | varies |
| `npid_get_allocated_memory` | VERIFIED | CBMC error |
| `npid_is_attached` | VERIFIED | CBMC error |
| `npid_is_attached_task` | VERIFIED | varies |
| `npid_print_usage` | VERIFIED | varies |
| `npid_add_allocated_memory` | FAIL (array_bounds.2) | CBMC error |
| `npid_dec_allocated_memory` | TIMEOUT | varies |

Coverage diagnostics flagged 7/11 functions as compile-error
because the full bmc-agent's preprocessing path differs from the
manual cpp preprocessing used by the trivial-spec sweeps.

## What the LLM specs looked like

Example for `npid_attach`:

```
PRE:  valid(nd) && valid_range(nd->attached_processes, 0, 16) &&
      (forall i, 0 <= i < 16 ==> valid(nd->attached_processes[i])) &&
      (the process calling this function is a valid running task)
POST: (ensures result == true ==> (exists slot, 0 <= slot < 16 &&
      nd->attached_processes[slot].pid == task_tgid_nr(current) &&
      nd->attached_processes[slot].open_count >= 1)) &&
      (ensures result == false ==> all slots full)
```

Much richer than `precondition = "true"`. But CBMC then has to
verify the `forall i over 16` clause and hits the unwind=4 bound,
yielding `unwind.4` FAIL.

## Net assessment

**For bug-finding on attack-surface kernel code, trivial-spec mode
is the right call.** CBMC's built-in pointer-check / bounds-check
property set catches the bug classes that matter (the
ncdev_bar_read OOB earlier was found this way) without needing
LLM-generated postconditions.

**Where the LLM pipeline would shine:** functional correctness on
algorithmic code with closed-form specs (the llm.c work earlier
this session). For "is this safe at attacker-controlled inputs",
raw CBMC is sufficient.

## Files

- `npid_*/spec.json` — LLM-generated pre/post for each function
- (bug_report.json + classification.json omitted; too large with
  preprocessed kernel TU embedded; reproducible by re-running the
  pipeline)
