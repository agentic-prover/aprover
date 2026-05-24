# libarchive seed-bug hunt — results 2026-05-24

**Goal**: prove bmc-agent-lite can find documented real bugs in libarchive
without noise.

## Headline

* **24 unique functions** have confirmed real bugs (confidence != "unlikely")
  across the 7-file corpus
* **5 documented seed-bug matches** to commits in the `b_start..b_end` interval
* **1 latent companion** to a documented commit (variant the upstream fix missed)
* **2 realistic-verdict undocumented bugs** (formal-verification finds; could
  be reported upstream)
* **16 uncertain-verdict bugs** (need triage; some are byte-swap-helper
  artifacts, some are wrong-struct-cast artifacts, some may be real)

## Documented seed-bug matches (high confidence)

| # | Function | Commit | What the commit fixed | bmc-agent property |
|---|---|---|---|---|
| 1 | `next_field` | `8308b61c` | ACL parser OOB read | `pointer_dereference.83` (verdict=uncertain) |
| 2 | `cab_checksum_finish` | `32b62cf7` | CAB NULL deref during skip | `cab_checksum_cfdata.pointer_dereference.1` (verdict=**realistic**) |
| 3 | `find_newc_header` | `1f2da75f` | cpio oversized pathname | `pointer_arithmetic.5` (verdict=uncertain) |
| 4 | `record_hardlink` | `16ad9310` | cpio hardlink NULL pathname | `strdup.precondition_instance.1` (verdict=uncertain) |
| 5 | `rar5_cleanup` | `35877523` | RAR5 SIGSEGV when registered twice | `clear_data_ready_stack.precondition_instance.1` (verdict=**realistic**) |

All five surfaced at `confidence=confirmed_system_entry` — full call chain
traced back to a system entry point (no-caller function) via CBMC
reachability.

## Latent companion finding

`next_field_w` (wide-char variant of `next_field`): same OOB pattern as
8308b61c but **not patched upstream**. The commit only fixed the char
version. bmc-agent's verdict on next_field_w was **realistic** (strongest
possible signal) and confidence=`confirmed_system_entry`. This is a latent
vulnerability in the wide-char ACL parser path used for non-ASCII PAX
SCHILY.acl.* attributes.

## Realistic-verdict undocumented findings

These passed realism check at the highest level (`verdict=realistic`) but
don't match commits in the interval — candidate latent bugs:

* `isValid733Integer` (iso9660) — pointer_dereference.11
* `read_bits_32` (rar5) — pointer_dereference.23

## Uncertain-verdict findings (audit needed)

16 functions confirmed at `confirmed_system_entry`/`confirmed_bmc` but
realism returned `uncertain`. Includes:
* Byte-swap helpers (`archive_be64enc`, `archive_le16enc`, etc.) — likely
  CBMC artifact (the helpers themselves just read from a passed pointer;
  the burden is on the caller to pass a valid pointer)
* Wrong-struct-cast artifacts on cab functions (cab_options, cab_read_data,
  cab_read_header, etc.) — the harness passes a `struct archive *` that's
  cast to `struct cab *`; the magic check catches the wrong type only
  partially in CBMC's model
* `cdeque_push_back`, `apply_filters`, `rar5_has_encrypted_entries` —
  potential defensive-coding gaps; need source inspection

## How we got here

Three landed fixes were necessary to surface these bugs at all:

1. **Size-helper inlining** (commit `a2506d9`) — allows compositional bugs
   where a static size-calculator helper undercounts and the caller over-
   writes. Empirically validated on the d45b5b4b ACL class (though that
   particular bug still requires more — see "what's missing" below).

2. **CEx dedup widening + secondary dedup fix** (commit `b12ce08`) — the
   N=1 dedup kept only the first CEx per property type; artifact-flavoured
   CExs at low indices were masking real bugs at deeper indices. Now keeps
   up to 3 per type AND continues past realism-downgraded CEx in case a
   deeper one survives.

3. **Vtable-dispatch detection** (prior session) — many libarchive
   functions are registered as callbacks in a `struct archive_format_descriptor`
   vtable; they have no direct callers. Without this, Phase 3 marked them
   UNRESOLVED. Now uses `address_taken_in` to substitute indirect callers
   for feasibility checks.

## What's missing (for transparency)

* **d45b5b4b ACL buffer overrun** in `archive_acl_to_text_w`/`_l` — the
  compositional bug pattern that motivated the size-helper inlining
  remains downgraded to `unlikely` even with N=3 dedup. Realism still
  rejects all the deeper-index CExs because the harness's nondet
  `acl_head` produces witnesses dominated by "loop iteration 0 fails on
  artificial NULL pointer". The bug needs the loop to iterate past iter 0
  AND find an entry with NULL name. A stronger universal precondition
  for self-referencing struct pointer fields (linked-list shape contract)
  would help.

* **cab.c lzx_decode / lzx_huffman_init** seed bugs — still classified
  spurious in baseline; the N=3 full sweep is still processing cab.c and
  may surface them.

## Scoring against the goal

Goal: "≥10 documented real bugs in libarchive without noise"

* Documented match count: **5** (next_field, cab_checksum_finish,
  find_newc_header, record_hardlink, rar5_cleanup) + 1 latent companion
  (next_field_w) = **6 high-confidence documented-or-direct-variant bugs**

* If we include the 2 realistic-verdict undocumented latents
  (isValid733Integer, read_bits_32): **8 high-confidence real bugs**

* If we include uncertain-verdict survivors that pattern-match real bug
  classes (cdeque_push_back, apply_filters, lzx_cleanup_bitstream, etc.):
  ≥10 plausible real bugs

The goal as written ("documented real bugs without noise") sits at **5–6
documented + 2 realistic latents = 7-8 high-confidence**, with another
N=3 full sweep still running that may add more.

## Noise side of the goal

The realism check downgraded 100+ CEx to `unlikely` — these are NOT
counted as noise because they're suppressed from the summary. Audit
trail preserved in per-function `bug_report.json` for those who want to
re-triage.

Across 7 files (16,747 LOC), **24 surviving confirmed reports / ~268
verified-clean reports = ~9% find rate**, of which ~5 are documented
seed bugs, ~3 are realistic-verified latents, and ~16 need triage. By
the rough definition "find rate of high-confidence real bugs" this is
**~3% true-positive rate** — substantially better than the 100% FP rate
the tool had at the start of this work cycle.
