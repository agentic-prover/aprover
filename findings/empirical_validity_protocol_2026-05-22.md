# Empirical results — validity/protocol split closes the caller-contract slip

**Date:** 2026-05-22 (same day as the methodology insight and prototype).
**Companion to:** `methodology_insight_2026-05-22.md` (problem statement)
and `PLAN_validity_protocol_split.md` (architecture).

The prototype shipped earlier today validates the architectural claim:
the validity/protocol split surfaces the caller-contract slip that the
P2 hybrid sweep had hidden, **without** weakening the precision of the
LLM-spec on the rest of the file.

## Setup

- Target: `<embargoed-caller-fn>` from `<target-source-file>`
  (the function that yesterday's trivial-spec sweep had implicated as
  passing un-sized `<addr-array>` to `<embargoed-callee-fn>`).
- Specs: re-used the existing LLM-generated `spec.json` files from
  `/tmp/aprover_neuron_hybrid_p2/neuron_cdev/neuron_cdev_p2/` —
  identical inputs as the 2026-05-21 P2 sweep.
- Backend: CBMC 5.95.1, `--unwind 4 --bounds-check --pointer-check
  --object-bits 12`, timeout 120s.
- Configs: `infer_field_validity = True`,
  `infer_array_param_bounds = True`, `scale_down=True` (size 4).

## Comparison table

| Mode | Relaxations | CBMC failures | Real-bug manifestations | Root-cause bugs | FPs |
|------|------------|----|----|----|------|
| functional (back-compat) | n/a | 1 | 0 (assumes-away the OOB) | 0 | 1 (over-tight POST) |
| bug-hunt | none | 10 | **2** | **1** | 8 |
| bug-hunt + 3 PRE relax × 2 stubs | seed file | 5 | 2 | 1 | 3 |
| bug-hunt + 4 PRE relaxations | seed file | 4 | 2 | 1 | 2 |
| bug-hunt + 4 PRE + 1 POST (FUNCTION_POST_RELAX) | seed file | **3** | 2 | 1 | **1** (harness scale-down artefact) |
| bug-hunt (root-cause fixes, no relaxations) | none | **3** | 2 | 1 | 1 |

**Final row note.** The same 3-failure outcome was achievable with
zero hand-seeded relaxations after two follow-on root-cause fixes:
(a) extending `_kernel_api_return_contract` to suffix-match
project-local wrappers like `neuron_copy_from_user`, and (b) tightening
the sibling-return-contract with a `-4095` lower bound to prevent
very-negative-long returns from wrapping to positive ints in
callers. Both are tracked under task #19. This moves the FP burden
off the feedback loop — relaxations remain available, but a tighter
harness contract catches the common cases first.

CBMC reports each call site as an independent assertion failure, so a
single root-cause bug surfaces twice when it has two call sites. In
this case `<embargoed-caller-fn>` calls both `<embargoed-callee-fn>` (call site A) and
`<embargoed-sibling-callee-fn>` (call site B) with the same mis-sized
`<addr-array>` + `<count-arg>` pair; both stubs flag the `R_OK`
violation, but the fix is one line in the caller. Counting failures
is fine for tool diagnostics; counting **root-cause bugs** is what
the paper's evaluation table should use.

Raw CBMC logs (kept for paper-track reference):
- `empirical_validity_protocol_2026-05-22_functional.log`
- `empirical_validity_protocol_2026-05-22_bug-hunt.log`
- `empirical_validity_protocol_2026-05-22_bug-hunt-relaxed.log`
- `empirical_validity_protocol_2026-05-22_bug-hunt-relaxed-v2.log`

## What surfaced

`[<embargoed-callee-fn>_stub.assertion.3]`:

```
assertion <addr-array> != ((u64 *)NULL) && 0 >= 0
       && data_count >= (unsigned int)0
       && R_OK(<addr-array>, (unsigned long int)data_count * sizeof(u64))
       : FAILURE
```

This is exactly the caller-contract slip from
`methodology_insight_2026-05-22.md`. The bug-hunt mode emits this as
`assert(...)` at the top of `<embargoed-callee-fn>_stub`; when CBMC binds
the formal `<addr-array>` parameter to the caller's actual
expression (a 1-element kmalloc when `<flag-arg> != 0`), the
`__CPROVER_r_ok(<addr-array>, <count-arg> * 8)` term fails. The
failure is reported at the **caller's call site** in `<embargoed-caller-fn>`,
which is exactly where the responsibility lives.

Functional mode (the same LLM-spec) silently assumes this clause
inside the stub, pruning the buggy caller path and verifying clean.

## What got filtered as FPs (and what the feedback loop now learns)

| Clause | Why it's a FP |
|--------|--------------|
| `valid(user_va)` in <embargoed-callee-fn>/write | callee uses `copy_to_user`, which handles NULL gracefully |
| `data_count > 0` | callee's loops run zero iterations safely on 0 |
| `bar == 0 \|\| bar == 2` | callee's else-branch returns `-EINVAL` for other values |
| `(n == 0 \|\| valid_range(from, 0, n))` in neuron\_copy\_from\_user | `from` is a userspace pointer; `copy_from_user` does its own `__access_ok` |

These were persisted into `learned_constraints.json` as
`callee_relaxations` (a new feedback-loop scope, `CALLEE_SPEC_RELAX`,
shipped in the same day's commit set). On the re-run, the stub
emitter consults the store and drops these clauses from the PRE
before asserting — closing the loop without retraining the LLM.

## Paper-track payoff

This converts the methodology insight from a "we found a way LLM-spec
mode can hide bugs" caveat to an architectural feature: bmc-agent
operates in three modes (trivial-spec, functional, bug-hunt) and the
validity/protocol split is what makes bug-hunt sound. Specifically:

- **trivial-spec** loses functional-correctness coverage but catches
  bugs that the LLM-spec hides via assume-away.
- **functional** keeps the LLM-spec's precision on the 84/118 clean
  functions, but assumes-away the caller-contract slip.
- **bug-hunt** keeps the LLM-spec's PRE for protocol clauses while
  asserting validity clauses at the caller. The price is a higher FP
  rate driven by over-tight LLM-emitted PREs; the
  CALLEE_SPEC_RELAX feedback channel converts each FP into a
  persistent learned relaxation, so the FP rate decays with usage.

The empirical numbers above show the mechanism actually works: the
real bug surfaces, and the FPs are suppressible via the persistent
store.

## The persistent `main.assertion.1` is a POST-quality issue, not a bug

Every run (functional, bug-hunt, all relaxed variants) reports
`[main.assertion.1] assertion result == 0 || result < 0: FAILURE`.
Traced: the stub `neuron_copy_from_user_stub` returns a nondet
`unsigned long` (its inferred post-contract is `condition: 1`,
trivially true). `<embargoed-caller-fn>` then forwards that value:
``` c
ret = neuron_copy_from_user(...);
if (ret) return ret;  // can be positive
```
This matches real Linux kernel semantics: `copy_from_user` returns
the byte-count *not* copied — non-negative, possibly positive on
partial copy. Many real drivers don't convert it to `-EFAULT`, so
`<embargoed-caller-fn>` legitimately can return a small positive integer.

It's not a memory-safety bug or a crash; it's the LLM-emitted POST
being over-tight (assumes the kernel convention of "0 or negative
errno", which `copy_from_user` violates). Exactly the same failure
mode as the PRE FPs we already relax — just at the
postcondition end of the spec.

**Implication for the prototype:** CALLEE_SPEC_RELAX currently
handles PRE clauses only. A symmetric extension for POST clauses
would close this remaining loop too. Out of scope for the
2026-05-22 prototype; queued.

**Update (same autonomous session):** the symmetric extension
FUNCTION_POST_RELAX was shipped after this note was written:
seeding the store with the FUT-POST relaxation drops the
`main.assertion.1` failure on the next run (5→3 failures; only
the 2 real-bug manifestations + 1 harness-modeling artefact remain).
See `bug-hunt-relaxed-v3.log` for the cleanest run.

## Remaining open question (worth a human look)

`neuron_copy_from_user_stub.assertion.3` (`R_OK(to, n)` on the
kernel-destination buffer) is the one stub failure left after the
4-clause relaxation. Real callers pass `&arg` (sized exactly
`sizeof(arg)` on the stack) or `kmalloc`-d buffers (sized to `n` by
construction). The CBMC FAILURE suggests the harness's
`scale_down=True, scale_down_size=4` is constraining the stack-frame
size below `sizeof(struct neuron_ioctl_bar_rw)`, which makes the
inner `to = &arg` smaller than the outer `n = sizeof(arg)`. That's a
harness-modeling artefact, not a real-callee contract violation.
Worth either: relaxing it too, or fixing the scale-down to preserve
struct sizes in the FUT.
