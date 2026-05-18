# pl2303_tiocmset / pl2303_dtr_rts — NULL-deref on disconnected device

**Verdict: NOT a real bug — verification artifact.**

Both candidate findings reduce to the same root pattern: a USB-serial
driver callback derefs the per-port private data without a defensive
NULL check. The kernel's USB-serial framework, however, enforces the
lifecycle invariant that makes `NULL` unreachable along any path the
framework can construct. The CBMC counterexample reaches the deref
only by violating an invariant the harness doesn't model.

## Source

* Driver: `drivers/usb/serial/pl2303.c` (Linux 7.1-rc4)
* Functions: `pl2303_tiocmset`, `pl2303_dtr_rts`
* AProver run: 2026-05-18, `/tmp/aprover_pl2303/`
* CBMC verdict: `verified=False`, `confirmed_dynamic` (SIGSEGV reproduced
  in the auto-generated GCC harness)
* Realism check: `uncertain` (auto-downgraded from a draft `realistic`
  verdict because the LLM's audit response didn't fit the REQ-1/REQ-2
  template the prompt requires)

## Counterexample-implied bug claim

The LLM's reasoning, paraphrased:

> `pl2303_tiocmset` is a TTY ioctl callback. It does
> `port = tty->driver_data` then `priv = usb_get_serial_port_data(port)`
> then `spin_lock_irqsave(&priv->lock, ...)`. There is no NULL check on
> `port` or `priv`. If a user-space process holds an open TTY fd while
> the PL2303 adapter is unplugged, a subsequent `ioctl(TIOCMSET)` can
> dispatch to `pl2303_tiocmset` with `tty->driver_data == NULL` (or
> `priv == NULL`), producing a kernel NULL-deref / panic.

`pl2303_dtr_rts` has the same shape: it's called from the USB-serial
core with a `struct usb_serial_port *port` argument, derefs `priv =
usb_get_serial_port_data(port)`, and locks `priv->lock` without checking
either pointer.

## Why this isn't reachable

### 1. The USB-serial framework wrapper itself assumes non-NULL.

`drivers/usb/serial/usb-serial.c::serial_tiocmset`:

```c
static int serial_tiocmset(struct tty_struct *tty, unsigned int set,
                           unsigned int clear)
{
    struct usb_serial_port *port = tty->driver_data;
    dev_dbg(&port->dev, "%s\n", __func__);          /* <-- derefs port */
    if (port->serial->type->tiocmset)
        return port->serial->type->tiocmset(tty, set, clear);
    return -ENOTTY;
}
```

If `tty->driver_data` were `NULL`, `dev_dbg(&port->dev, ...)` would crash
*before* the call to `pl2303_tiocmset`. The framework's `dev_dbg`
expansion takes `&port->dev` — an offset-from-NULL address. So the
framework itself relies on `tty->driver_data != NULL`. This isn't a
weak assumption; it's the documented TTY-layer contract: the layer
sets `driver_data` during `tty->ops->install` (here: `serial_install`)
and prevents any other op from running until install completes.

### 2. `priv` is set during `pl2303_port_probe` and not cleared until `pl2303_port_remove`.

`pl2303_port_probe`:
```c
priv = kzalloc(sizeof(*priv), GFP_KERNEL);
if (!priv) return -ENOMEM;
spin_lock_init(&priv->lock);
init_waitqueue_head(&priv->delta_msr_wait);
priv->type = ...;
usb_set_serial_port_data(port, priv);            /* <-- sets driver_data */
```

`pl2303_port_remove`:
```c
struct pl2303_private *priv = usb_get_serial_port_data(port);
kfree(priv);                                      /* <-- frees but doesn't NULL */
```

Between `port_probe` returning success and `port_remove` running, `priv`
is a valid pointer. The framework guarantees no `tiocmset` call before
`port_probe` succeeds (the port is not visible to userspace) and no
`tiocmset` call after `port_remove` completes (the TTY is detached via
`tty_port_close` / `tty_release` in the disconnect path — the
`tty->driver_data` reference is dropped before the port struct is
released).

### 3. The disconnect race is fenced by `tty_port`/`usb_serial_disconnect` locks.

`drivers/usb/serial/usb-serial.c::usb_serial_disconnect` acquires
`serial->disc_mutex` and calls `tty_port_tty_hangup` on each port,
which sets the TTY's `hung_up` state. After hangup, the TTY layer
short-circuits ioctls before they reach `serial_tiocmset` — they fail
with `-EIO`/`-ENODEV`. The remaining vector (an ioctl arriving in the
narrow window between USB disconnect-notification and the hangup
completing) is fenced by `serial->disc_mutex` and `port->mutex` (held
during open/close paths).

There's no observed runtime path where `pl2303_tiocmset` runs with
`tty->driver_data == NULL` or `priv == NULL`. Adding a defensive NULL
check would be cosmetic — there is nothing for the check to catch in
practice.

## Why CBMC found it anyway

The bmc-agent harness for `pl2303_tiocmset`:

* Sets `tty` as nondet (with all 36 struct fields unconstrained).
* Does NOT model `tty_port_initialized`, `port->serial->disconnected`,
  the `disc_mutex` ordering, or the TTY-layer contract that
  `driver_data` is non-NULL when ops are dispatched.
* Lets CBMC explore the path `tty->driver_data == NULL` → `port ==
  NULL` → `&port->dev` → `dev_get_drvdata(NULL)` → deref.

This is exactly the false-positive class CBMC produces on system code
without an environmental model — well-known, documented in the LWN
"CBMC for kernel code" articles and in past `syzkaller + KASAN` audits
that disprove similar reports.

## Why the realism check didn't filter it

* Pre-classifier witness-pattern detectors (library-init globals NULL,
  path-divergent unwind, jv stub disconnect, the NULL-guard early-return
  added this session for ch341) don't match this counterexample
  because the function has NO explicit guard to detect.
* The LLM realism check's `auto-downgraded` mechanism caught that the
  LLM hadn't followed the REQ-1/REQ-2 template, so it landed in
  `uncertain` rather than `realistic`. That's the *system* doing
  defensive downgrading, not a substantive disagreement.

## Recommendation

* **No code change to pl2303 needed.** The pattern is safe under the
  framework's lifecycle invariants.
* **Pipeline-side improvement** (next item in the bmc-agent backlog):
  add a `[usb-serial-framework-invariant]` witness detector that
  recognises `tty->driver_data == NULL` (and similar `port == NULL`)
  on USB-serial callbacks listed in `struct usb_serial_driver` and
  auto-rejects as UNREALISTIC. This is the symmetric pattern to the
  `_LIBRARY_INIT_GLOBALS` detector — for any framework whose
  registration shape AProver can recognise, the framework's lifecycle
  guarantees can be encoded as pre-LLM filters.

## Cross-link

This finding will be reported in the AProver paper as a *negative*
case-study: when bmc-agent runs on a well-tested upstream Linux
driver with no real bugs, the false-positive pressure all comes from
unmodelled framework lifecycle guarantees. The fix is to add those
guarantees to the pre-classifier, not to add NULL checks to the
driver code.
