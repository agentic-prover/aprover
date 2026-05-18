# dp83tc811_set_wol — NULL deref of `phydev->attached_dev`

**Verdict: candidate hardening bug. Realistic in form (missing defensive
NULL check), unlikely to be exploitable along framework-constructed
paths, but present in three sibling PHY drivers as a family.**

## Summary

`dp83811_set_wol` in `drivers/net/phy/dp83tc811.c` dereferences
`phydev->attached_dev` without a NULL check. If a caller invokes the
function while `phydev->attached_dev == NULL`, the kernel panics on the
`mac = (const u8 *)ndev->dev_addr` access.

The same shape exists in `dp83822_config_wol` and `dp83867_set_wol`.
None of the three TI PHY drivers guard `attached_dev`.

## Source

* Driver: `drivers/net/phy/dp83tc811.c` (Linux 7.1-rc4)
* Function: `dp83811_set_wol`
* Recent activity: file last touched 2026-05-12 (`net: phy: DP83TC811:
  add reading of abilities`), so this is current-tree code.
* AProver run: 2026-05-18, `/tmp/aprover_dp83tc811/`
* CBMC verdict: `verified=False`, `counterexamples=43`
* Dynamic replay: `confirmed_dynamic` (SIGSEGV captured in
  auto-generated GCC harness)
* LLM realism audit: `realistic`

## The defective pattern

```c
static int dp83811_set_wol(struct phy_device *phydev,
                           struct ethtool_wolinfo *wol)
{
    struct net_device *ndev = phydev->attached_dev;
    const u8 *mac;
    ...
    if (wol->wolopts & (WAKE_MAGIC | WAKE_MAGICSECURE)) {
        mac = (const u8 *)ndev->dev_addr;        /* <-- NULL deref if ndev == NULL */
        if (!is_valid_ether_addr(mac))
            return -EINVAL;
        ...
    }
}
```

No `if (!ndev) return -EINVAL;` guard before the `ndev->dev_addr` read.
The `is_valid_ether_addr(mac)` check fires *after* the dereference, so
it does not protect this path.

## Family-wide pattern

Identical missing guard in two siblings:

* `dp83822_config_wol` (drivers/net/phy/dp83822.c):
  ```c
  struct net_device *ndev = phydev->attached_dev;
  ...
  if (wol->wolopts & (WAKE_MAGIC | WAKE_MAGICSECURE)) {
      mac = (const u8 *)ndev->dev_addr;
      if (!is_valid_ether_addr(mac))
          return -EINVAL;
  ```

* `dp83867_set_wol` (drivers/net/phy/dp83867.c):
  ```c
  struct net_device *ndev = phydev->attached_dev;
  ...
  if (wol->wolopts & WAKE_MAGIC) {
      mac = (const u8 *)ndev->dev_addr;
      if (!is_valid_ether_addr(mac))
          return -EINVAL;
  ```

This is not a one-off oversight — it is an idiom that propagated across
the TI PHY driver family. Likely cause: cut-and-paste from a common
template.

## Reachability analysis

Where can `phydev->attached_dev == NULL` occur?

* `phy_attach_direct(dev, phydev, ...)` sets `attached_dev = dev` only
  when `dev != NULL`. If `dev == NULL` is passed (legal per the
  conditional `if (dev) { phydev->attached_dev = dev; ... }`), the
  attach succeeds with `attached_dev` left as whatever it was, typically
  NULL after `phy_register`.
* `phy_detach` explicitly clears `attached_dev = NULL`.
* All in-tree callers of `phy_attach_direct` in Linux 7.1-rc4
  (`phy_connect_direct`, `phylink_attach_phy`, `hns_enet`, `xgbe-phy-v2`,
  `phylink.c` x2) pass a real `dev`, so `attached_dev != NULL` after
  attach succeeds.
* `phy_ethtool_set_wol`, the documented framework wrapper, checks
  `phydev->drv && phydev->drv->set_wol` but does NOT check
  `phydev->attached_dev`. It then calls the driver's `set_wol`.

Practical exploit paths:

1. **Out-of-tree driver** that calls `phy_attach_direct(NULL, phydev,
   ...)` and later invokes `phy_ethtool_set_wol` (or the callback
   directly) on the un-netdev-bound phydev. Reachable in principle, not
   observed in upstream code.
2. **Race** between `phy_detach` and an in-flight ethtool ioctl. The
   ethtool path holds a `dev_hold` reference on the netdev, which is
   typically detached *before* `phy_detach` runs, so this is normally
   ordered. Not impossible in driver shutdown corner cases.
3. **Direct driver-internal call** to `dp83811_set_wol(phydev, wol)`
   on a phydev that has not yet been attached (e.g., from a probe-time
   self-test). Unusual but not prohibited.

In short: not a directly weaponisable bug along the common ethtool
path, but a real defensive-coding gap. The fact that
`phy_attach_direct` explicitly handles `dev == NULL` indicates the
framework considers that an allowed configuration; any driver
callback that derefs `attached_dev` without a guard is unsafe in
that configuration.

## Recommendation

A two-line fix per driver:

```c
static int dp83811_set_wol(struct phy_device *phydev,
                           struct ethtool_wolinfo *wol)
{
    struct net_device *ndev = phydev->attached_dev;
    const u8 *mac;

    if (!ndev)
        return -ENODEV;

    if (wol->wolopts & (WAKE_MAGIC | WAKE_MAGICSECURE)) {
        ...
    }
}
```

Same patch shape for `dp83822_config_wol` and `dp83867_set_wol`.

## bmc-agent pipeline behaviour

This finding exercised the full pipeline end to end on a kernel TU:

1. **Phase 1**: spec generated as
   `precondition: valid(phydev) && valid(wol) && phydev is attached to
   a properly initialized net_device with a valid MAC address` (LLM's
   inferred precondition explicitly mentions attached-to-net_device but
   the CBMC harness treats `phydev->attached_dev` as nondet so the
   constraint is not active).
2. **Phase 2 (Stage 1 reachability)**: CBMC found 43 counterexamples,
   `verified=False`.
3. **Phase 3 (Stage 2 feasibility + Stage 3 dynamic)**: GCC harness
   reproduced SIGSEGV on the witness inputs.
4. **Stage 4 realism audit**: LLM verdict `realistic` (Q1: NULL-deref
   class is real; Q2: function reachable via ethtool `.set_wol`
   callback registration; no guard in the function body).
5. **Final tier**: `confirmed_dynamic` (Stage 3 captured SIGSEGV on a
   non-assertion property).

## Caveats

* The realism check rejected the matching SEMANTIC postcondition
  failures on `dp83811_config_init`, `dp83811_config_intr`, and the
  postcondition-side failure on `dp83811_set_wol` as `unrealistic`
  (stub returns unconstrained). Only the memory-safety counterexample
  survived the audit.
* The MEMORY_SAFETY witness sets `phydev->attached_dev = NULL`. The
  USB-serial-framework-invariant detector (added 2026-05-18 for the
  pl2303 finding) does NOT match this case, because the registration
  shape is `struct phy_driver` not `struct usb_serial_driver`. An
  analogous PHY-framework detector — recognising
  `attached_dev == NULL` as framework-managed when the phy is attached
  through `phy_attach_direct(non-NULL, ...)` — would be a symmetric
  improvement, though the framework guarantee for PHYs is weaker than
  USB-serial's (since `phy_attach_direct(NULL, ...)` is allowed).

## Cross-link

* Compare to `pl2303_tiocmset_finding.md`: that one was also a missing
  NULL check on a framework-managed pointer, but the USB-serial
  framework's lifecycle invariants make the NULL state truly
  unreachable, so it was classified as NOT a real bug. The PHY
  framework's lifecycle is looser — `phy_attach_direct(NULL, ...)` is
  an explicit allowed configuration — so the same shape here is closer
  to a real hardening gap.
