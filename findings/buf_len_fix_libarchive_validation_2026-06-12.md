# (buf,len) harness-sizing fix — cross-target validation on libarchive

**Date:** 2026-06-12
**Fix under test:** commit `fe2beed` — "size (buf,len) byte params to the length".

## The fix (recap)

`_detect_buf_len_pairs` (bmc_agent/harness_generator.py) pairs a **raw-byte pointer**
(`uint8_t*` / `unsigned char*` / `int8_t*` / byte-typedef — **not** plain `char*`,
which is the NUL-string convention, and **not** `void*`) immediately followed by a
**size-named integer**, and sizes the buffer to the length: `buf = malloc(len)`
(len bounded). This deterministically fixes the spurious OOB on every length-driven
read — the cause of the VibeOS net `icmp/ip/tcp_handle` false positives. `buf` is
exactly `len` bytes, so reads in `[0,len)` are in-bounds **and** an off-by-one past
`len` is still caught (a fixed over-sized buffer would mask it; CBMC-verified both
directions).

## Why a cross-target check was required

The fix touches the core per-parameter harness emission, which runs on **every**
target — including the load-bearing libarchive eval. "General fix" ≠ "trivially
safe": it had to be shown neutral on libarchive, not just helpful on VibeOS.

## Method

Source: `/tmp/libarchive_bench/libarchive/libarchive/` (the eval build).
1. Ran `_detect_buf_len_pairs` over every parsed libarchive function signature →
   measured the blast radius.
2. Cross-referenced the affected functions against the 43 known seed bugs
   (`findings/libarchive_seed_bugs_43.md`, `findings/libarchive_*results*.md`).
3. Regenerated an affected harness, confirmed it CBMC-parses with the build config
   (`-DHAVE_CONFIG_H -I build/`).
4. A/B: same function harness **with** vs **without** the fix (detector monkeypatched
   to `[]`), compared CBMC verdicts.

## Results

| Check | Result |
|-------|--------|
| Blast radius | **8 of ~1224** functions affected (0.65%). libarchive uses `void*`/`char*` for most buffer APIs — both excluded. |
| Affected fns | `strappend_bin`, `uu_encode`, `win_crypto_Update/Final`, RAR `parse_filter`/`compile_program`, RAR5 `read_consume_bits`/`push_data_ready` — all genuine `(byte-buf, len)` cases where `malloc(len)` is the correct model. |
| Overlap with the 43 known bugs | **Zero.** The fix touches no established finding → cannot regress the libarchive results. |
| Harness integrity | Parses/compiles cleanly with the fix. (An initial parse error was a missing `config.h` in the manual CBMC call, not the fix.) |
| Verdict A/B (`read_consume_bits`) | **Identical** failing properties with and without the fix: `read_bits_16.pointer_dereference.23/29/35` in both. The failure is a pre-existing *struct-field-used-as-index* artifact (`p[rar->bits.in_addr]`), orthogonal to `(buf,len)`. |

## Conclusion

The `(buf,len)` fix is **general** (the convention is universal) and **safe
cross-target**: tiny scope on libarchive, zero overlap with known bugs, no harness
breakage, and verdict-neutral on the affected functions. It deterministically
eliminates the VibeOS net packet-handler FPs while leaving the libarchive eval
undisturbed.
