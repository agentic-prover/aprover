# rtl8125_tool_ioctl — u32 wraparound in bounds check (real bug)

**Verdict: real integer-overflow bug.** The bounds check on user-supplied
`offset + len` is performed in `u32` arithmetic, which wraps modulo 2^32.
A privileged caller (`CAP_NET_ADMIN`) can pass values whose sum wraps
below the resource length, pass the check, and then trigger an MMIO
read or write at `mmio_addr + offset` with an arbitrary 32-bit offset —
far outside the BAR mapping.

This is an out-of-tree vendor driver, not mainline. The mainline `r8169`
driver does not implement this ioctl. Severity is bounded by
`CAP_NET_ADMIN`; effect is kernel-side OOB MMIO read / write, typically
an oops or corruption of unrelated I/O memory.

## Source

* Driver: `realtek-r8125-dkms` (OOT), file `src/rtltool.c`
* Function: `rtl8125_tool_ioctl(struct rtl8125_private *tp, struct ifreq *ifr)`
* Cases affected: `RTLTOOL_READ_MAC` (line 59), `RTLTOOL_WRITE_MAC` (line 82)
* AProver run: 2026-05-18, `/tmp/aprover_rtltool/`
* Preprocessed input: `/tmp/rtl_oot/realtek-r8125-dkms/src/rtltool.i`

## The bug

`struct rtltool_cmd` (rtltool.h:72):

```c
struct rtltool_cmd {
    __u32 cmd;
    __u32 offset;
    __u32 len;
    __u32 data;
};
```

`rtl8125_tool_ioctl`, `RTLTOOL_READ_MAC` case (rtltool.c:58-79):

```c
case RTLTOOL_READ_MAC:
    if ((my_cmd.offset + my_cmd.len) > pci_resource_len(tp->pci_dev, 2)) {
        ret = -EINVAL;
        break;
    }

    if (my_cmd.len==1)
        my_cmd.data = readb(tp->mmio_addr + my_cmd.offset);
    else if (my_cmd.len==2)
        my_cmd.data = readw(tp->mmio_addr + (my_cmd.offset & ~1));
    else if (my_cmd.len==4)
        my_cmd.data = readl(tp->mmio_addr + (my_cmd.offset & ~3));
    ...
```

`offset` and `len` are both `__u32`. Their sum is computed in `unsigned int`
and wraps mod 2^32. Picking `offset = 0xFFFFFFFC, len = 4` makes the sum
`0`, which is `<= pci_resource_len(tp->pci_dev, 2)` for any nonzero resource.
The check passes. The subsequent `readl(tp->mmio_addr + (offset & ~3))`
then reads from `mmio_addr + 0xFFFFFFFC`, ~4 GiB past the BAR base.

The identical pattern in `RTLTOOL_WRITE_MAC` (rtltool.c:82) lets the same
trick *write* arbitrary 32-bit values to an arbitrary 32-bit offset of MMIO
space.

## Fix

Standard overflow-safe bounds check:

```c
u32 reslen = pci_resource_len(tp->pci_dev, 2);
if (my_cmd.len > reslen || my_cmd.offset > reslen - my_cmd.len) {
    ret = -EINVAL;
    break;
}
```

Or, equivalently, `if (my_cmd.offset >= reslen || my_cmd.len > reslen - my_cmd.offset)`.

## Reachability

The handler is reached from userspace via:

```
ioctl(fd, SIOCRTLTOOL, ifr)
  → r8125_n.c:rtl8125_do_ioctl(dev, ifr, SIOCRTLTOOL)   (r8125_n.c:16154)
      → capable(CAP_NET_ADMIN)                          (r8125_n.c:16155)
      → rtl8125_tool_ioctl(tp, ifr)                     (r8125_n.c:16160)
```

`CAP_NET_ADMIN` is required, so an unprivileged process cannot trigger it.
A local attacker with `CAP_NET_ADMIN` (typically root, NetworkManager, or
processes granted the cap explicitly) can:

* Oops the kernel on demand (DoS), or
* Read 1/2/4 bytes from arbitrary 32-bit MMIO offsets (info leak against
  whatever else is mapped in that ioremap window — usually returns
  `0xFF`/garbage on x86 ioremap, but device-dependent), or
* Write 1/2/4 bytes to arbitrary 32-bit MMIO offsets (potential corruption
  of any device whose registers happen to be mapped contiguously, though
  this is unusual on modern kernels where ioremap regions are scattered).

## How bmc-agent surfaced it

This finding was first noticed during manual triage of CBMC's NULL-deref
counterexamples on `rtl8125_tool_ioctl` (the framework-invariant
``tp->pci_dev`` FP class — same as the pl2303 and dp83tc811 findings).
CBMC's default invocation does not flag the integer overflow because
unsigned wrap is defined C behaviour.

After three pipeline-level fixes were shipped (commit-batch
``2026-05-18 OOT-r8125``), bmc-agent now **surfaces this bug
autonomously** when run with ``--enable-flag-selection``:

1. **Parser fix.** ``_collect_struct_defs`` recurses into
   ``function_definition`` / ``compound_statement`` / ``ERROR`` parse-
   recovery wrappers. On large preprocessed kernel TUs tree-sitter
   buries top-level structs (including ``struct rtl8125_private``)
   inside phantom function bodies after a parse error; without the
   recurse, the struct never reached ``struct_definitions`` and the
   harness fell back to a flat ``struct rtl8125_private _tp_val;`` with
   nondet back-pointers.

2. **Harness fix.** ``_emit_struct_field_init`` emits
   ``__CPROVER_assume(field != NULL && __CPROVER_r_ok(field, sizeof(*field)))``
   on the netdev/PCI driver-private back-pointers
   (``pci_dev``, ``netdev``, ``pdev``, ``mii_bus``, ``mmio_addr``, and
   ``dev`` when typed as ``net_device`` / ``device`` / ``pci_dev``).
   These are framework invariants set unconditionally by ``probe()``
   before ``register_netdev`` makes the device dispatchable.

3. **DSL fix.** ``translate_atom`` splits on top-level ``&&`` before the
   ``locked()`` ghost-comment fallback, so a precondition like
   ``valid(tp) && !locked(tp->phy_lock)`` keeps the translatable
   ``valid(tp)`` clause instead of being dropped wholesale. The
   sanitiser's "invented field" heuristic no longer matches the ``addr``
   suffix (real kernel fields ``mmio_addr``, ``phy_addr``, ``mac_addr``
   are no longer dropped), and any clause that does get commented out
   has its ``*/`` markers escaped so the outer ``/* ghost: … */``
   wrapper can't be prematurely terminated.

With those in place and ``--enable-flag-selection`` enabled, the
flag-selector LLM picks ``--unsigned-overflow-check`` and
``--pointer-overflow-check`` for ``rtl8125_tool_ioctl`` (threat-model:
security; function reads ``copy_from_user``'d data then uses it as an
offset). CBMC then asserts the OOB pointer arithmetic on
``(char *)tp->mmio_addr + (signed long int)my_cmd.offset`` (property
``rtl8125_tool_ioctl.pointer_arithmetic.5``) and the unsigned addition
overflow on ``my_cmd.offset + my_cmd.len`` (property
``rtl8125_tool_ioctl.overflow.1``). The realism check confirms ``real_bug``
on entry-function semantics (no in-tree callers above; reachable
directly from system boundary via ``ioctl(SIOCRTLTOOL)``).

The previously-FP NULL-deref on ``tp->pci_dev`` is now ruled out by the
back-pointer assume, so the overflow finding is no longer hidden behind
a framework-invariant artifact.

## Reporting

Upstream is the realtek-r8125-dkms maintainers' repo on GitHub (multiple
forks; the most-active is `awesometic/realtek-r8125`). The mainline
kernel `r8169` driver does not implement `SIOCRTLTOOL` and is unaffected.
