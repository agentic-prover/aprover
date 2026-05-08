# BUG-22 — `strncpy` (string)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/string.c` |
| **Realism** | uncertain |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid_range(dest, 0, n) && valid_string(src) && (n == 0 || dest != NULL) && (the memory regions dest[0..n) and src[0..strlen(src)] do not overlap)`

**Postcondition:** `\result == dest && (forall i, 0 <= i < n && i < strlen(src) ==> dest[i] == src[i]) && (forall i, strlen(src) <= i < n ==> dest[i] == '\0') && (strlen(src) < n ==> dest[strlen(src)] == '\0')`

## Counterexample

**Violated property:** `strncpy.unwind.0`

**Key variable assignments:**
```
_dest_buf = <symbolic struct/array — see classification.json>
_dest_len = 2u
_dest_buf[2l] = 16
_dest_buf[0l] = ' '
_dest_buf[1l] = ' '
_dest_buf[3l] = ' '
_dest_buf[4l] = 0
dest = _dest_buf!0@1
_src_buf = <symbolic struct/array — see classification.json>
_src_len = 4u
_src_buf[4l] = 0
_src_buf[0l] = ' '
_src_buf[1l] = ' '
_src_buf[2l] = 16
_src_buf[3l] = ' '
src = _src_buf!0@1
n = 9223372036854775808ul
result = ((char *)NULL)
return_value_strncpy = ((char *)NULL)
i = 4ul
```

## Root cause

CBMC reports a `strncpy.unwind.0` failure — a semantic / contract violation in `strncpy`.

**Realism checker's key concern:** The specific n=2^63 witness is a CBMC artifact from unconstrained symbolic execution of dead code. However, the underlying vulnerability class (OOB write when n greatly exceeds dest's actual allocation) is real and exploitable if this function is ever called with an externally-derived, unchecked n value.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`strncpy` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The specific n=2^63 witness is a CBMC artifact from unconstrained symbolic execution of dead code. However, the underlying vulnerability class (OOB write when n greatly exceeds dest's actual allocation) is real and exploitable if this function is ever called with an externally-derived, unchecked n value.

Q1 (Can the violation TYPE occur?): The violated property is a loop-unwind bound (strncpy.unwind.0). The underlying concern is: with a very large `n`, the second zero-filling loop (`for (; i < n; i++) dest[i] = '\0';`) runs for an astronomical number of iterations. If `n` is attacker-controlled and large while `dest` is a bounded buffer, this results in a massive out-of-bounds write (memory safety violation) and/or an effective denial-of-service. The loop technically always terminates (bounded by `n`), but with n=2^63 it is practically infinite. The vulnerability class — passing an untrusted, unbounded `n` to `strncpy` causing OOB writes — is a real and well-known class of bug. Q2 (Are the specific witness values achievable?): The specific value n=9223372036854775808 (2^63) is the result of unconstrained symbolic execution with no callers constraining `n`. No real caller is shown to pass such a value. The function is identified as dead code with no call sites. The witness is almost certainly a CBMC symbolic extreme, not a realistic scenario. However, an attacker could plausibly supply a large-but-not-astronomical `n` via an API that derives the count from external input (e.g., from a length field in a network packet), causing OOB writes proportional to the difference between `n` and `strlen(src)`. The specific witness is synthetic, but the bug class is real if any future caller passes externally-derived `n` without bounding it to `dest`'s allocation size.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
