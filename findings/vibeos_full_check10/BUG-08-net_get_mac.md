# BUG-08 — `net_get_mac` (net)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/net.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(mac, 0, 6)`

**Postcondition:** `ensures valid_range(mac, 0, 6) && the 6 bytes at mac[0..5] contain the current network interface MAC address`

## Counterexample

**Violated property:** `net_get_mac.precondition_instance.3`

**Key variable assignments:**
```
our_mac = <symbolic struct/array — see classification.json>
our_mac[0l] = 0
our_mac[1l] = 0
our_mac[2l] = 0
our_mac[3l] = 0
our_mac[4l] = 0
our_mac[5l] = 0
_mac_val = 0
mac = _mac_val!0@1
```

## Root cause

CBMC reports a `net_get_mac.precondition_instance.3` failure — a semantic / contract violation in `net_get_mac`.

**Validator reasoning:** 'net_get_mac' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`net_get_mac` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

Q1: Yes, the violation type (null pointer dereference via memcpy on a NULL mac pointer) can absolutely occur. The function net_get_mac accepts a uint8_t* mac parameter and immediately passes it to memcpy with no NULL guard. If any caller passes NULL, memcpy will dereference address 0, causing undefined behavior (typically SIGSEGV). Since no callers are found in the codebase and this is treated as a public API entry point, any external caller — including attacker-controlled code or network-facing components — could trivially pass NULL. Q2: Yes, the specific witness value (mac = NULL/0) is entirely achievable in real execution. There is nothing special or impossible about passing NULL to a pointer parameter. The dynamic harness confirmed this by reproducing a SIGSEGV signal when mac = NULL is passed. The combination of: (a) no NULL check, (b) public API with no constraints on callers, (c) confirmed dynamic reproduction all point to a realistic exploitable null dereference.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
