# VibeOS Uncertain-Realism Bug Findings — `vibeos_full_check10` run

26 bugs from the bmc-agent run `vibeos_full_check10`
(May 7–8 2026) on the [VibeOS](https://github.com/notgull/vibeos) kernel
where the realism checker returned **`uncertain`** — the violation type is
plausible but the witness or call-site context could not be fully ruled in or out.

These need careful human triage: any one of them could be a true bug the
checker simply lacked context to confirm, or a false positive masquerading as
a borderline case. The matched-realistic subset lives in
[`../vibeos_full_check10/`](../vibeos_full_check10/).

## Summary

| # | Function | Module | Tier | Signal | Bug type | Status |
|---|---|---|---|---|---|---|
| [BUG-01](BUG-01-rtc_init.md) | `rtc_init` | rtc.c | `confirmed_dynamic` | SIGSEGV | memory_safety | ☐ |
| [BUG-02](BUG-02-kbd_ring_push.md) | `kbd_ring_push` | usb_hid.c | `confirmed_dynamic` | SIGABRT | semantic | ☐ |
| [BUG-03](BUG-03-console_init.md) | `console_init` | console.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-04](BUG-04-newline.md) | `newline` | console.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-05](BUG-05-fat32_is_dir.md) | `fat32_is_dir` | fat32.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-06](BUG-06-read16.md) | `read16` | fat32.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-07](BUG-07-keyboard_getc.md) | `keyboard_getc` | keyboard.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-08](BUG-08-memory_heap_start.md) | `memory_heap_start` | memory.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-09](BUG-09-mouse_get_screen_pos.md) | `mouse_get_screen_pos` | mouse.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-10](BUG-10-stbtt_GetGlyphHMetrics.md) | `stbtt_GetGlyphHMetrics` | ttf.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-11](BUG-11-stbtt_PackBegin.md) | `stbtt_PackBegin` | ttf.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-12](BUG-12-stbtt_PackFontRangesPackRects.md) | `stbtt_PackFontRangesPackRects` | ttf.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-13](BUG-13-stbtt__buf_get8.md) | `stbtt__buf_get8` | ttf.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-14](BUG-14-stbtt__buf_peek8.md) | `stbtt__buf_peek8` | ttf.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-15](BUG-15-stbtt__buf_range.md) | `stbtt__buf_range` | ttf.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-16](BUG-16-usb_reenable_channel.md) | `usb_reenable_channel` | usb_transfer.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-17](BUG-17-vfs_append.md) | `vfs_append` | vfs.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-18](BUG-18-vfs_close_handle.md) | `vfs_close_handle` | vfs.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-19](BUG-19-fat_name_to_str.md) | `fat_name_to_str` | fat32.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-20](BUG-20-wsod_draw_text.md) | `wsod_draw_text` | irq.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-21](BUG-21-hal_serial_putc.md) | `hal_serial_putc` | serial.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-22](BUG-22-strncpy.md) | `strncpy` | string.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-23](BUG-23-apply_italic.md) | `apply_italic` | ttf.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-24](BUG-24-stbtt__close_shape.md) | `stbtt__close_shape` | ttf.c | `confirmed_bmc` | — | arithmetic | ☐ |
| [BUG-25](BUG-25-stbtt__handle_clipped_edge.md) | `stbtt__handle_clipped_edge` | ttf.c | `confirmed_bmc` | — | memory_safety | ☐ |
| [BUG-26](BUG-26-ttf_init.md) | `ttf_init` | ttf.c | `confirmed_bmc` | — | arithmetic | ☐ |

## Confidence tiers

- `confirmed_dynamic` — runtime fault (SIGSEGV/SIGABRT/etc.) confirmed by GCC-compiled reproducer execution
- `confirmed_system_entry` — full call chain traced to a system entry point; not dynamically executed
- `confirmed_bmc` — CBMC formal model violation; reachability confirmed by call-graph analysis only

## How to review

For each bug:
1. Read the call chain and locate the function in `examples/vibeos/repo/kernel/`
2. Verify the counterexample variable assignments are reachable
3. Cross-check the realism reasoning
4. Update the Status column (☐ → ✓ confirmed / ✗ false positive)

## Source of truth

Underlying artifacts (CBMC harnesses, raw counterexamples, classification JSON,
dynamic harness sources) live in `artifacts/vibeos_full_check10/vibeos_kernel/`
and are gitignored — re-run bmc-agent to regenerate.
