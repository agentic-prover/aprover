# VibeOS Realistic Bug Findings — `vibeos_full_check10` run

34 **realistic** bugs from the bmc-agent run `vibeos_full_check10`
(May 7–8 2026) on the [VibeOS](https://github.com/notgull/vibeos) kernel.

Filter applied: every entry's realism checker verdict is `realistic`
(`unrealistic` and `uncertain` reports excluded).

Sibling views of the same run:
- [`../vibeos_full_check10_uncertain/`](../vibeos_full_check10_uncertain/) — 26 reports the realism checker flagged `uncertain` and need triage
- [`../vibeos/`](../vibeos/) — earlier 13-bug curation from a prior run (not consistent with this one; see commit history)

## Summary

| # | Function | Module | Tier | Signal | Bug type | Status |
|---|---|---|---|---|---|---|
| [BUG-01](BUG-01-hal_dma_fb_copy.md) | `hal_dma_fb_copy` | platform.c | `confirmed_dynamic` | SIGABRT | arithmetic | ☐ |
| [BUG-02](BUG-02-kapi_delete.md) | `kapi_delete` | kapi.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-03](BUG-03-kapi_file_size.md) | `kapi_file_size` | kapi.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-04](BUG-04-kapi_get_datetime.md) | `kapi_get_datetime` | kapi.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-05](BUG-05-kapi_rename.md) | `kapi_rename` | kapi.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-06](BUG-06-keyboard_irq_handler.md) | `keyboard_irq_handler` | keyboard.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-07](BUG-07-mouse_set_pos.md) | `mouse_set_pos` | mouse.c | `confirmed_dynamic` | SIGABRT | arithmetic | ☐ |
| [BUG-08](BUG-08-net_get_mac.md) | `net_get_mac` | net.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-09](BUG-09-hal_usb_get_device_info.md) | `hal_usb_get_device_info` | platform.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-10](BUG-10-stbtt_FreeShape.md) | `stbtt_FreeShape` | ttf.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-11](BUG-11-stbtt_GetBakedQuad.md) | `stbtt_GetBakedQuad` | ttf.c | `confirmed_dynamic` | SIGSEGV | memory_safety | ☐ |
| [BUG-12](BUG-12-stbtt_GetFontVMetrics.md) | `stbtt_GetFontVMetrics` | ttf.c | `confirmed_dynamic` | SIGSEGV | arithmetic | ☐ |
| [BUG-13](BUG-13-stbtt_GetKerningTableLength.md) | `stbtt_GetKerningTableLength` | ttf.c | `confirmed_dynamic` | SIGSEGV | arithmetic | ☐ |
| [BUG-14](BUG-14-stbtt_GetPackedQuad.md) | `stbtt_GetPackedQuad` | ttf.c | `confirmed_dynamic` | SIGSEGV | memory_safety | ☐ |
| [BUG-15](BUG-15-ttUSHORT.md) | `ttUSHORT` | ttf.c | `confirmed_dynamic` | SIGSEGV | memory_safety | ☐ |
| [BUG-16](BUG-16-hal_usb_mouse_poll.md) | `hal_usb_mouse_poll` | usb_hid.c | `confirmed_dynamic` | SIGSEGV | semantic | ☐ |
| [BUG-17](BUG-17-read32.md) | `read32` | fat32.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-18](BUG-18-write32.md) | `write32` | fat32.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-19](BUG-19-malloc.md) | `malloc` | memory.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-20](BUG-20-hal_get_time_us.md) | `hal_get_time_us` | platform.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-21](BUG-21-stbtt_GetFontBoundingBox.md) | `stbtt_GetFontBoundingBox` | ttf.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-22](BUG-22-stbtt_GetNumberOfFonts_internal.md) | `stbtt_GetNumberOfFonts_internal` | ttf.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-23](BUG-23-stbtt_GetScaledFontVMetrics.md) | `stbtt_GetScaledFontVMetrics` | ttf.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-24](BUG-24-stbtt_PackEnd.md) | `stbtt_PackEnd` | ttf.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-25](BUG-25-stbtt_ScaleForMappingEmToPixels.md) | `stbtt_ScaleForMappingEmToPixels` | ttf.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-26](BUG-26-stbtt_ScaleForPixelHeight.md) | `stbtt_ScaleForPixelHeight` | ttf.c | `confirmed_system_entry` | — | arithmetic | ☐ |
| [BUG-27](BUG-27-stbtt__isfont.md) | `stbtt__isfont` | ttf.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-28](BUG-28-ttULONG.md) | `ttULONG` | ttf.c | `confirmed_system_entry` | — | memory_safety | ☐ |
| [BUG-29](BUG-29-hal_usb_keyboard_poll.md) | `hal_usb_keyboard_poll` | usb_hid.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-30](BUG-30-vfs_read.md) | `vfs_read` | vfs.c | `confirmed_system_entry` | — | semantic | ☐ |
| [BUG-31](BUG-31-elf_process_relocations.md) | `elf_process_relocations` | elf.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-32](BUG-32-wsod_draw_line.md) | `wsod_draw_line` | irq.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-33](BUG-33-stbtt__CompareUTF8toUTF16_bigendian_prefix.md) | `stbtt__CompareUTF8toUTF16_bigendian_prefix` | ttf.c | `confirmed_bmc` | — | semantic | ☐ |
| [BUG-34](BUG-34-stbtt_setvertex.md) | `stbtt_setvertex` | ttf.c | `confirmed_bmc` | — | arithmetic | ☐ |

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
