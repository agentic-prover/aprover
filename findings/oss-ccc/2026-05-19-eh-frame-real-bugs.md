# claudes-c-compiler backend/linker_common/eh_frame.rs — 4 real bugs

**Date**: 2026-05-19
**Source**: anthropics/claudes-c-compiler `master` checkout 2026-05-19,
`src/backend/linker_common/eh_frame.rs`
**Target functions**: 12 declared
**bmc-agent config**: Kani backend, `--threat-model security`. LLM:
`anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**4 real Rust panics confirmed in the LE byte readers/writers
embedded in the DWARF unwind-frame handler.** Same anti-pattern as
`backend/elf/io.rs`:

| Function | Bug class |
|---|---|
| `read_u32_le` | slice OOB on `data[offset+N]` (N=0..3) |
| `read_i32_le` | same, signed |
| `read_u64_le` | slice OOB on `data[offset+N]` (N=0..7) |
| `write_i32_le` | slice OOB on destination write |

The `.eh_frame` section in an attacker-controlled `.o` file drives
the linker through this code. A malformed `.eh_frame` with crafted
offsets makes the linker crash instead of producing a clean error.

## Why these are separate functions from elf/io.rs

The file has its own private copies of `read_*_le` / `write_*_le`
helpers rather than reusing `elf/io.rs`. They share the bug class
because they share the implementation anti-pattern — direct
`data[offset+N]` indexing with no bounds check.

A defensive refactor would consolidate both to one set of
`Result`-returning helpers in `elf/io.rs` and remove the
duplicate-shaped panic sites here.

## Other functions in the file

The DWARF-specific parsers (`read_uleb128`, `read_sleb128`,
`count_eh_frame_fdes`, `parse_cie_fde_encoding`, `decode_eh_pointer`,
`eh_pointer_size`) verified clean or got Kani timeouts on heavy
inner loops over byte streams. The 4 bugs above are all in the
byte-helper layer.

`parse_eh_frame_fdes` and `build_eh_frame_hdr` exhausted Kani's
retry chain at the smallest slice bound and produced TIMEOUT
verdicts (no real bug found, but no clean verify either).

## Classification

All 4 REAL_BUG under `--threat-model security`; LATENT under
safety/functional.

## bmc-agent improvement landed

None eh_frame.rs-specific; surfaced via the same defensive spec
workflow as `elf/io.rs`. The Phase 3c re-verification chain (timeout
→ unwind-bump → shrink-bound) consumes most of the runtime here
because the DWARF parser fns iterate over variable-length byte
streams; the byte-helper bugs themselves land quickly.
