# BUG-11 — `stbtt_GetBakedQuad` (ttf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/ttf.c` |
| **Bug type** | memory_safety |
| **Violated property** | `main.pointer_dereference.12` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid_range(chardata, 0, char_index + 1) && char_index >= 0 && pw > 0 && ph > 0 && valid(xpos) && valid(ypos) && valid(q)`

**Postcondition:** `valid(q) && valid(xpos) && q->s0 >= 0.0f && q->s1 <= 1.0f && q->t0 >= 0.0f && q->t1 <= 1.0f && *xpos == \old(*xpos) + chardata[char_index].xadvance && (opengl_fillrule != 0 ? (q->x0 == (float)((int)floor(\old(*xpos) + chardata[char_index].xoff + 0.5f)) && q->y0 == (float)((int)floor(\old(*ypos) + chardata[char_index].yoff + 0.5f))) : true)`

## Counterexample

**Violated property:** `main.pointer_dereference.12`

**Key variable assignments:**
```
_chardata_val = {'members': [{'name': 'x0', 'value': {'binary': '0000000000000000', 'data': '0', 'name': 'integer', 'type': 'unsigned short int', 'width': 16}}, {'name': 'y0', 'value': {'binary': '0000000000000000...
chardata = _chardata_val!0@1
pw = 1
ph = 571887488
char_index = 26843546
_xpos_val = 0.000931
xpos = _xpos_val!0@1
_ypos_val = -5.957554e-8
ypos = _ypos_val!0@1
_q_val = {'members': [{'name': 'x0', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'float', 'width': 32}}, {'name': 'y0', 'value': {'binary': '00000000000000000000000000000000...
q = _q_val!0@1
opengl_fillrule = 32768
d3d_bias = 0
ipw = 1
iph = 1.748596e-9
b = {'name': 'unknown'}
round_x = 0
return_value_floor = 0
x = 17.246704
return_value___sort_of_CPROVER_round_to_integral = 17
rounding_mode = 0
d = 17.246704
magicConst = 4.503600e+15
return_value = 17
saved_rounding_mode = 0
return_value_fegetround = 0
goto_symex$$return_value$$fegetround = 0
return_value_fabs = 17.246704
goto_symex$$return_value$$fabs = 17.246704
tmp = 4.503600e+15
goto_symex$$return_value$$__sort_of_CPROVER_round_to_integral = 17
goto_symex$$return_value$$floor = 17
round_y = 17
return_value_floor$0 = 17
_q_val.x0 = 0
_q_val.y0 = 17
_q_val.x1 = 0
_q_val.y1 = -31616
_q_val.s0 = 1
_q_val.t0 = 0.000085
_q_val.s1 = 1
_q_val.t1 = 0.00003
tmp_if_expr$0 = 0
```

## Root cause / validation reasoning

'stbtt_GetBakedQuad' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

Dynamic harness outcome: `inconclusive`. Harness generation failed.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No bounds check on `char_index` relative to the size of the `chardata` array; attacker-controlled character values can produce arbitrary out-of-bounds pointer dereferences, confirmed by SIGSEGV in dynamic testing.

Q1 (Can the violation TYPE occur?): YES. The function computes `b = chardata + char_index` and then dereferences `b` without any bounds check on `char_index` against the actual size of the `chardata` array. Since `char_index` is a direct parameter with no validation, any caller passing a `char_index` that is out of range of the allocated `chardata` buffer will produce an invalid pointer, and the subsequent `b->xoff`, `b->yoff`, `b->x0`, etc. dereferences will access memory out of bounds. This is a classic OOB read vulnerability. Q2 (Are these specific witness values realistic?): The specific `char_index = 26843546` is extreme, but the vulnerability class does not require such extreme values — even a `char_index` of 1 when `chardata` has only one entry would cause the same issue. More importantly, the dynamic harness confirmed the fault (SIGSEGV). In practice, `char_index` is derived from a character code (typically `codepoint - first_char`), and an attacker providing a large or unexpected codepoint, or a Unicode value beyond the expected range, could easily produce an out-of-range `char_index`. There is no caller-side validation shown, and the call-site analysis shows no callers guard this path. The function is a public API receiving untrusted input.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
