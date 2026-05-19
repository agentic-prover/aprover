# claudes-c-compiler backend/elf/io.rs — 11 real bugs

**Date**: 2026-05-19
**Source**: anthropics/claudes-c-compiler `master` checkout 2026-05-19,
`src/backend/elf/io.rs`
**Target functions**: 19 declared, 11 with real bugs after classifier improvements
**bmc-agent config**: Kani backend, `--threat-model security`. LLM:
`anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**11 real Rust panics confirmed across the ELF byte-IO helpers.**
All 11 share the same anti-pattern: a `pub fn helper(buf: &[u8],
offset: usize, ...) -> T` (or `&mut [u8]`) that indexes
`buf[offset]`, `buf[offset+N]`, or arithmetic on `offset + N` with
no bounds / overflow check and no documented precondition.

The functions are reachable from the linker stage when CCC reads
attacker-controlled `.o` / `.a` files. Every in-tree caller passes
"reasonable" offsets derived from already-validated header fields,
so the panics aren't triggered by normal compilation flow — but a
crafted ELF object with malformed offsets crashes the linker.

## Bugs by class

### Class A — slice OOB on byte readers (5 fns)

| Function | Body | Panic site |
|---|---|---|
| `read_u16` | `u16::from_le_bytes([data[offset], data[offset + 1]])` | `data[offset+1]` OOB |
| `read_u32` | 4-byte LE read | `data[offset+N]`, N=0..3 |
| `read_u64` | 8-byte LE read | `data[offset+N]`, N=0..7 |
| `read_i32` | signed variant of `read_u32` | same |
| `read_i64` | signed variant of `read_u64` | same |

**Panic property**: `slice_index_fail.do_panic.runtime.assertion.N`.

**Implicit contract** the LLM correctly inferred but didn't enforce:
`offset + N <= data.len()` where N ∈ {2, 4, 8}.

### Class B — usize overflow in bounds checks (3 fns)

```rust
pub fn w16(buf: &mut [u8], off: usize, val: u16) {
    if off + 2 <= buf.len() {              // overflow: off near usize::MAX
        buf[off..off + 2].copy_from_slice(&val.to_le_bytes());
    }
}
```

| Function | Failing check |
|---|---|
| `w16` | `off + 2 <= buf.len()` overflows |
| `w32` | `off + 4 <= buf.len()` overflows |
| `w64` | `off + 8 <= buf.len()` overflows |

The bounds check itself overflows in release builds, bypassing the
guard. In debug builds Rust panics on the overflow (which is what
Kani found). Either way the function misbehaves on adversarial
offsets — debug panics, release silently bypasses.

**Implicit contract**: `off + N <= isize::MAX` (or equivalently
`off <= usize::MAX - N`).

### Class C — slice OOB / overflow on phdr helpers (3 fns)

`wphdr`, `write_phdr64`, `write_bytes` delegate to / extend Class A
+ B operations. Same unchecked arithmetic; same panic patterns. The
phdr writers issue a 56-byte structured write at `off..off+56` via
multiple `w32`/`w64` calls; each inherits its constituent's lack of
guards.

## Classification

All 11 are **REAL_BUG under `--threat-model security`** (attacker can
supply a malformed ELF and drive panics through the public API), and
**LATENT under safety/functional** (no in-tree crash path on
well-formed objects).

## Fix sketch

```rust
pub fn read_u16(data: &[u8], offset: usize) -> Result<u16, ElfError> {
    let end = offset.checked_add(2).ok_or(ElfError::OffsetOverflow)?;
    if end > data.len() {
        return Err(ElfError::Truncated);
    }
    Ok(u16::from_le_bytes([data[offset], data[offset + 1]]))
}
```

Two changes: explicit overflow-safe bounds check, and a `Result` return
so the caller is forced to handle truncated input. Callers then write
`read_u16(data, off)?` and a malformed `.o` produces a clean error
message instead of a crash.

## bmc-agent improvement landed

None elf/io.rs-specific; the bugs surfaced via the standard defensive
spec workflow combined with the **LATENT classification + threat-model
gate** added this session (commits `c3864af` + `975c1cd`). Before those
fixes, several of these were silently downgraded to SPURIOUS after Phase
3c refinement; the threat-model security gate correctly promotes them
to REAL_BUG.
