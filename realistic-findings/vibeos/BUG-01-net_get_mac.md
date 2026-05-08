# BUG-01 — `net_get_mac` (net)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/net.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no callers)

## Spec (LLM-generated)

**Precondition:** `requires valid_range(mac, 0, 6)`

**Postcondition:** `ensures valid_range(mac, 0, 6) && the 6 bytes at mac[0..5] contain the current network interface MAC address`

## Counterexample

**Violated property:** `net_get_mac.precondition_instance.3`

**Key variable assignments:**
```
mac = NULL (0)
our_mac[0..5] = 0
```

## Root cause

`net_get_mac` copies the six-byte MAC address directly into the caller-provided buffer without any NULL check on the `mac` parameter. When `mac` is NULL, writing `mac[0]` through `mac[5]` dereferences a null pointer and causes SIGSEGV. A single `if (!mac) return;` guard would prevent the crash.

The function is a public API entry point with no visible call sites in the codebase, meaning external callers cannot be assumed to always supply a valid non-NULL buffer.

## How to trigger

Call `net_get_mac(NULL)` from any code that invokes the networking API, for example during an error-handling path or initialization sequence where the caller fails to allocate a receive buffer before polling the MAC address.

## Realism assessment

**Verdict:** REALISTIC

1. FUNCTION NATURE: net_get_mac is identified as a public API entry point with no call sites found in the file. This means external callers (other compilation units, external libraries, or user code) may invoke it with arbitrary inputs.

2. COUNTEREXAMPLE: mac = NULL (0). The function blindly passes mac to memcpy without any NULL validation. If any caller passes NULL — whether accidentally, during error-handling paths, or during initialization sequences — the function will crash.

3. DYNAMIC CONFIRMATION: The harness confirmed SIGSEGV with mac=NULL. This is a direct, reproducible crash, not a speculative violation.

4. CALL-SITE ANALYSIS: No callers are visible in the file, meaning we cannot assert 'all real callers always pass valid pointers.' For a public API, this is a significant concern — external consumers may not know the implicit contract requires a non-NULL pointer, especially since the function signature has no documentation or annotation indicating this.

5. PATTERN ANALYSIS: In embedded/network code, MAC address buffers are sometimes conditionally allocated or passed from partially initialized structures. A NULL mac pointer could arise from a failed allocation, an uninitialized struct member, or a coding error in external consumer code.

6. MISSING GUARD: The function has zero defensive checks. A single NULL check (if (!mac) return;) would prevent the crash. The absence of this guard, combined with the public-API nature of the function, makes this a real latent defect.

7. NOT A FALSE POSITIVE: The guidance for false positives requires call-site analysis showing all callers pass valid non-NULL values. Here, no such constraint exists — callers are unconstrained.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
