# VibeOS Confirmed Bug Findings

13 confirmed bugs found by bmc-agent on the [VibeOS](https://github.com/notgull/vibeos) kernel
(~15,000 lines, bare-metal ARM64).  Run: `vibeos_full_check5`, all validation tiers enabled.

## Summary

| # | Function | Module | Tier | Signal | Realism | Status |
|---|---|---|---|---|---|---|
| [BUG-01](BUG-01-net_get_mac.md) | `net_get_mac` | net.c | `confirmed_dynamic` | SIGSEGV | realistic | ☐ |
| [BUG-02](BUG-02-stbtt__buf_get.md) | `stbtt__buf_get` | ttf (stb_truetype) | `confirmed_dynamic` | SIGSEGV | realistic | ☐ |
| [BUG-03](BUG-03-stbtt__h_prefilter.md) | `stbtt__h_prefilter` | ttf (stb_truetype) | `confirmed_dynamic` | SIGABRT | realistic | ☐ |
| [BUG-04](BUG-04-stbtt_PackEnd.md) | `stbtt_PackEnd` | ttf (stb_truetype) | `confirmed_dynamic` | SIGABRT | realistic | ☐ |
| [BUG-05](BUG-05-hal_usb_keyboard_poll.md) | `hal_usb_keyboard_poll` | usb_hid.c | `confirmed_dynamic` | SIGSEGV | realistic | ☐ |
| [BUG-06](BUG-06-vfs_lookup.md) | `vfs_lookup` | vfs.c | `confirmed_system_entry` | — | realistic | ☐ |
| [BUG-07](BUG-07-vfs_open_handle.md) | `vfs_open_handle` | vfs.c | `confirmed_system_entry` | — | realistic | ☐ |
| [BUG-08](BUG-08-vfs_close_handle.md) | `vfs_close_handle` | vfs.c | `confirmed_system_entry` | — | realistic | ☐ |
| [BUG-09](BUG-09-strtok_r.md) | `strtok_r` | string.c | `confirmed_system_entry` | — | realistic | ☐ |
| [BUG-10](BUG-10-hal_serial_getc.md) | `hal_serial_getc` | serial.c | `confirmed_system_entry` | — | realistic | ☐ |
| [BUG-11](BUG-11-align4.md) | `align4` | dtb.c | `confirmed_system_entry` | — | realistic | ☐ |
| [BUG-12](BUG-12-stbtt_GetPackedQuad.md) | `stbtt_GetPackedQuad` | ttf (stb_truetype) | `confirmed_system_entry` | — | uncertain | ☐ |
| [BUG-13](BUG-13-stbtt__csctx_rmove_to.md) | `stbtt__csctx_rmove_to` | ttf (stb_truetype) | `confirmed_bmc` | — | realistic* | ☐ |

\* Only reachable when a CFF/OpenType font is loaded (default VibeOS font is TrueType).

## Confidence tiers

- `confirmed_dynamic` — runtime fault (SIGSEGV/SIGABRT) confirmed by GCC-compiled harness execution
- `confirmed_system_entry` — full call chain traced to a system entry point; not dynamically executed
- `confirmed_bmc` — CBMC formal model violation; reachability confirmed by call-graph analysis only

## How to review

For each bug:
1. Read the call chain and locate the function in `examples/vibeos/repo/kernel/`
2. Verify the counterexample variable assignments are reachable
3. Check the "How to trigger" section for a concrete reproduction path
4. Update the Status column (☐ → ✓ confirmed / ✗ false positive)
