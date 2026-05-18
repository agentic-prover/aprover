# dp83tc811_set_wol — NULL deref of `phydev->attached_dev`

**Verdict: NOT a real bug — verification artifact.**

Same shape as `pl2303_tiocmset_finding.md`: a missing defensive NULL
check on a framework-managed pointer. CBMC's nondet harness reaches a
NULL deref by violating an invariant the PHY framework maintains.

## Source

* Driver: `drivers/net/phy/dp83tc811.c` (Linux 7.1-rc4)
* Function: `dp83811_set_wol`
* File last touched: 2026-05-12 (`net: phy: DP83TC811: add reading of
  abilities`).
* AProver run: 2026-05-18, `/tmp/aprover_dp83tc811/`
* CBMC verdict: `verified=False`, `counterexamples=43`
* Dynamic replay: `confirmed_dynamic` (SIGSEGV captured in
  auto-generated GCC harness)
* LLM realism audit: `realistic` (incorrect; LLM speculated about a
  race condition that the framework's locking rules out)

## Counterexample-implied bug claim

The witness sets `phydev->attached_dev = NULL`, then
`dp83811_set_wol(phydev, wol)` with `wol->wolopts & WAKE_MAGIC` runs:

```c
struct net_device *ndev = phydev->attached_dev;   /* ndev = NULL */
const u8 *mac;
...
if (wol->wolopts & (WAKE_MAGIC | WAKE_MAGICSECURE)) {
    mac = (const u8 *)ndev->dev_addr;             /* NULL deref */
    ...
}
```

No `if (!ndev) return -EINVAL;` guard. The dynamic harness reproduces
the SIGSEGV.

## Why this isn't reachable

The PHY framework lifecycle establishes `attached_dev != NULL`
before any `set_wol` dispatch the framework can construct:

1. **`phy_ethtool_set_wol` is the documented framework wrapper**
   (`drivers/net/phy/phy.c:2027`) called from every in-tree ethtool
   path. It is reached only via `netdev->ethtool_ops->set_wol`, which
   requires a netdev. The netdev's `phydev` pointer is set by
   `phy_attach_direct` *only in the same `if (dev) { ... }` block that
   sets `attached_dev`*:

   ```c
   if (dev) {
       phydev->attached_dev = dev;
       dev->phydev = phydev;
       ...
   }
   ```

   So `dev->phydev == phydev` implies `phydev->attached_dev == dev`
   (both non-NULL). The ethtool path therefore enters
   `phy_ethtool_set_wol` with `attached_dev != NULL` by construction.

2. **All in-tree callers of `phy_attach_direct` pass a non-NULL
   `dev`**: `phylink.c:2220`, `phylink.c:2324`, `phy_device.c:1234`,
   `hns_enet.c:1181`, `xgbe-phy-v2.c:983`. None pass `NULL`. The
   `if (dev)` guard exists for an explicitly-allowed but in-tree-
   unused configuration.

3. **The `phy_detach`/`set_wol` race is fenced by `dev_hold`.**
   ethtool holds a reference on the netdev for the duration of the
   ioctl. `phy_detach` only runs after the netdev's `phydev`
   reference is dropped (in driver shutdown), so a concurrent
   ethtool ioctl cannot observe `attached_dev == NULL` mid-dispatch.

4. **Sibling drivers** (`dp83822_config_wol`, `dp83867_set_wol`)
   share the same defensive-check-free pattern, which is evidence
   that the kernel maintainers consider the framework-invariant
   sufficient and have not requested NULL-guard hardening.

The CBMC counterexample reaches `attached_dev = NULL` only by
violating the framework lifecycle invariant the harness doesn't
model.

## Why CBMC + LLM realism flagged it anyway

* The harness models `struct phy_device` as a nondet struct, so
  `phydev->attached_dev` is unconstrained — NULL is in its value set.
* No `dp83811_set_wol` body code excludes NULL.
* The LLM realism audit treated the missing guard plus the
  `set_wol` callback registration as sufficient evidence for
  REALISTIC, citing a hypothetical race condition. The lock-ordering
  argument above rules that out, but the LLM did not have
  `phy_ethtool_set_wol`'s wrapper code in its context window to
  cross-check.

## Why the witness-pattern pre-classifier didn't filter it

The USB-serial-framework-invariant detector (added 2026-05-18 for the
pl2303 finding) recognises only `struct usb_serial_driver` dispatch
tables. PHY drivers use `struct phy_driver`, so the detector did not
match. This is the symmetric gap the next pipeline-side improvement
addresses (see below).

## Recommendation

* **No code change to `dp83tc811`/`dp83822`/`dp83867` needed.** The
  pattern is safe under the framework's lifecycle invariants.
* **Pipeline-side improvement** (shipped alongside this re-classification):
  add a `[phy-framework-invariant]` witness-pattern detector that
  recognises `phydev->attached_dev == NULL` (and `phydev == NULL`) on
  functions registered as callback slots in a `struct phy_driver`
  dispatch table, and auto-rejects as UNREALISTIC. Symmetric to the
  USB-serial detector. Same general principle: when AProver can
  recognise a framework's registration shape, the framework's
  lifecycle guarantees can be encoded as pre-LLM filters.

## Cross-link

This finding will be reported in the AProver paper as a *second*
negative case-study, complementing
`pl2303_tiocmset_finding.md`. Both are valuable not as bugs (they
aren't) but as canonical examples of a recurring false-positive class:
**missing defensive NULL check on a framework-managed pointer**.
The fix for AProver is to recognise the framework, not to recommend a
patch to the driver.
