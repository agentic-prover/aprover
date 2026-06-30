# Confirmed bugs — 2026-05-22

Consolidated bug-finding output from today's session across two
projects: AWS Neuron driver (continuing yesterday's work) and
libarchive @ b_start (new interval-benchmark direction).

## Counting convention

Three tiers:

1. **PoC-confirmed** — dynamic reproducer exists (KASAN, libFuzzer
   crash, etc.). Strongest evidence; ready for upstream disclosure.
2. **Hindsight-confirmed** — REALISTIC static finding at b_start
   that maps to an upstream fix commit somewhere in `b_start..b_end`.
   Evidence of methodology soundness, not new bugs.
3. **Latent REALISTIC candidates** — REALISTIC after the LLM realism
   filter, with NO matching fix in the interval. Possible unreported
   bugs; need per-finding diff-check + manual triage + PoC before
   any disclosure conversation.

## Numeric summary

| Tier | AWS Neuron | libarchive | Total |
|---|---:|---:|---:|
| PoC-confirmed | 0 | 0 | **0** |
| Hindsight-confirmed | 0 | 6 | **6** |
| Latent REALISTIC candidates | 1 | 16 | **17** |
| **Total REALISTIC** | **1** | **22** | **23** |

Other relevant aggregates:

- Raw FAILs surfaced (across both projects, all sweep variants):
  ~370 (libarchive: 268; Neuron bug-hunt: ~100).
- Effective precision after realism filter: ~8% (22 REALISTIC /
  268 libarchive raw FAILs).
- Realism filter UNREALISTIC rate (i.e. raw FP rate at the
  function-flag level): ~89% on libarchive.

## Tier 2 — Hindsight-confirmed (6)

REALISTIC at b_start AND maps to a fix commit in `b_start..b_end`.

| # | Target | Function | Property class | Seed commit |
|---|---|---|---|---|
| H1 | libarchive | `contrib/untar.c::parseoct` | `*p` OOB read on n==0 | `00640329` (Pilot replicated mid-session) |
| H2 | libarchive | `archive_pathmatch::pm_list` | `*p++` past `end` | `4cbf9582` (heap over-read) |
| H3 | libarchive | `archive_pathmatch::pm_list_w` | same family (wide-char) | `4cbf9582` |
| H4 | libarchive | `archive_pathmatch::pm_slashskip_w` | `s[1]` past NUL | `4cbf9582` |
| H5 | libarchive | `iso9660::parse_rockridge_TF1` | OOB on `isodate17` deref | sibling of `c3cb1c56` (ZF1 fix) |
| H6 | libarchive | `iso9660::parse_rockridge_SL1` | unbounded `nlen` advance | sibling of `c3cb1c56` |

Plus from yesterday's reasoning-model pass (replaced by OR pass; included for
completeness of the seed mapping):

- `iso9660::build_pathname_utf16be` → `750e8d7b` (Joliet pathname overflow)
- `archive_acl::is_nfs4_flags_w` / `ismode` → `8308b61c` (ACL parser OOB)

## Tier 3 — Latent REALISTIC candidates (17)

### AWS Neuron driver (1)

- **N1.** One heap-OOB-read candidate identified via IOCTL path. Source-
  audit case is substantive (triple-corroborated static signal: trivial-spec
  sweep, LLM-spec bug-hunt mode at caller, bug-hunt at the parallel
  write-path). Details, trigger, and PoC sketch are embargoed in
  `<embargoed-findings-repo>` under
  `findings/aws_neuron_driver/unconfirmed/`.
  - Status: UNCONFIRMED. Path to PoC: KASAN reproducer on a Trainium /
    Inferentia host or QEMU + neuron driver build.

### libarchive @ b_start (16)

#### RAR5 family (9)

These cluster around the RAR5 bit-stream / decompression code. Many
share a "caller-controlled offset reads past block boundary" shape.

- **L1.** `rar5::add_new_filter` — NULL deref of `rar` in
  `cdeque_push_back` call. The function takes a precomputed
  `cdeque *` that can be NULL.
- **L2.** `rar5::bid_sfx` — pointer arithmetic `p + 8` and
  `buff + bytes_avail` violate bounds when `bytes_avail` is small
  or `p` advances near the buffer end.
- **L3.** `rar5::circular_memcpy` — negative `(end - start)` cast
  to `size_t` produces a huge memcpy length → buffer overflow.
- **L4.** `rar5::decode_number` — `array_bounds.1` failure. Indexes
  `table->decode_num[pos]` after clamping `pos`, but never validates
  `table->size > 0`. Empty table → out-of-bounds index.
- **L5.** `rar5::read_bits_16` — checks `in_addr >= cur_block_size`
  but then accesses `p[in_addr]`, `p[in_addr+1]`, `p[in_addr+2]` —
  3-byte OOB read possible.
- **L6.** `rar5::read_bits_32` — same pattern, 5-byte OOB read past
  the `in_addr` boundary check.
- **L7.** `rar5::read_filter_data` — caller-controlled offset feeds
  `circular_memcpy`, no bound on `offset+4 <= window_mask`.
- **L8.** `rar5::run_delta_filter` — caller-controlled `dest_pos`
  computed from `flt->block_length` and `flt->channels`, no bounds
  check before indexing `rar->cstate.filtered_buf[dest_pos]`.

#### RAR family (1)

- **L9.** `rar::read_exttime` — `localtime(&tm)` / `localtime_r` can
  return NULL on invalid `time_t` (out-of-range), but the result is
  dereferenced as `tm->tm_sec` without a NULL check.

#### ZIP family (3)

- **L10.** `zip::archive_read_format_zip_read_data_skip_streamable` —
  the loop condition `p <= buff + bytes_avail - 16` underflows when
  `bytes_avail < 16`; the early-return guard isn't airtight on all
  code paths.
- **L11.** `zip::rsrc_basename` — pointer arithmetic
  `name_length - (s - name)` underflows when `s` advances beyond
  `name + name_length`, then `memchr` is called with a huge length
  and can read out of bounds.
- **L12.** `zip::zipx_zstd_init` — `ZSTD_createDStream()` can return
  NULL on allocation failure, but the next call dereferences the
  return value without a NULL check.

#### ISO9660 family (2)

- **L13.** `iso9660::isodate17` — caller can pass NULL, function
  dereferences `v[0..16]`.
- **L14.** `iso9660::isNull` (via `memcmp.precondition.2`) — function
  does not validate that `h + offset` is within bounds before passing
  to `memcmp`; caller-controlled offset can cause OOB.

#### MTREE family (1)

- **L15.** `mtree::readline` — the loop
  `for (u = mtree->line.s + find_off; *u; ++u)` accesses `u[1]` and
  `u[2]` without bounds checking when `u[0] == '\\'`. Past-NUL read
  when string ends with a single trailing backslash.

#### archive_string (1)

- **L16.** `archive_string::strncat_from_utf8_libarchive2` —
  pre-extends destination for original `len` bytes + NUL, but
  `wcrtomb`/`wctomb` for converting invalid UTF-8 sequences can
  generate more bytes than `len`. Write up to `MB_CUR_MAX` past
  the bound check.

## What's NOT counted

- **MTREE hex parser** (seed `b2ce282d`) — logic error (hex digit
  miscount), not a memory-safety property CBMC's bounds-check or
  pointer-check would fire on. Static analysis with intrinsic
  properties cannot find this class.
- **7zip seed bugs (5)** — file failed to compile due to a parser
  quirk (orphan `else if` extracted as top-level forward decl).
  Toolchain blocker, not a detection limit.
- **XAR seed bugs (2)** — file failed to preprocess (missing
  `libxml/xmlreader.h`). Toolchain blocker.
- **RAR5 SIGSEGV-twice** (`35877523`) — verified clean by the
  static finder. Bug surfaces only on a re-init path the bounded
  harness's input space doesn't reach.

## Disclosure status

- **Neuron:** N1 has a draft disclosure template (embargoed in the
  private companion repo). Pending KASAN PoC before sending to AWS
  security.
- **libarchive latents (L1–L16):** No disclosures drafted. Each
  needs per-finding diff-check (does the fix really not exist?) +
  manual code-level triage + ideally a fuzzing reproducer before
  any disclosure conversation.

## Artifacts

- Per-finding LLM realism output:
  `findings/libarchive_realism_filter_openrouter_2026-05-22.json`
- Sweep aggregate:
  `/tmp/libarchive_sweep_2026-05-22/aggregate.json`
- Per-file scorecards:
  `/tmp/libarchive_sweep_2026-05-22/<file_stem>/scorecard.json`
- Companion writeup: `findings/libarchive_sweep_b_start_2026-05-22.md`
- Methodology background: `findings/methodology_insight_2026-05-22.md`,
  `findings/empirical_validity_protocol_2026-05-22.md`
