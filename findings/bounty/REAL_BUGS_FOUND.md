# Real bugs found by bmc-agent on bounty-eligible OSS

This file logs **confirmed** real bugs (not LLM-realism false positives or
harness artifacts) found while running bmc-agent against bounty-eligible
open-source projects. Each entry must be manually verified against source
and deduped against published CVEs/GHSAs.

## Entry format

```
### <target>::<function> — <property>

- Found: YYYY-MM-DD
- bmc-agent commit: <sha>
- Driver run: <path>
- LLM realism verdict: <verdict>
- Counterexample (key state): ...
- Manual triage:
  - Is the witness reachable through a public API? <yes/no + reasoning>
  - Is there a precondition check the function relies on? <which one>
  - Is there a NULL guard / bounds check upstream? <which one>
- CVE dedup: <list of similar CVEs checked, with note on why this is distinct>
- Bug class: <UB-pointer / OOB-read / integer-overflow / use-after-free / ...>
- Severity estimate: <low/medium/high>
- PoC needed: <UBSan / AddressSanitizer / crafted input>
- Reported: <where, when>
```

## Run log (negative results)

Negative-result runs (sweeps that found 0 real bugs after triage) are
kept here for traceability. They're not failures — confirming a
heavily-fuzzed codebase against bmc-agent specs is a positive paper-track
result.

### protobuf upb — `upb/wire/reader.c` (2026-05-12)
- 3 leaf parsers tested (`_upb_WireReader_ReadLongVarint/Tag/Size`)
- Pipeline reached CBMC end-to-end via shim
- 5 findings, all spec/stub artifacts
- 0 real bugs (consistent with OSS-Fuzz coverage)

### OpenSSL ASN.1 — `crypto/asn1/asn1_lib.c` (2026-05-12)
- 24 functions, --real-libc --strict-dsl
- 15 verified clean (including all DER length decoders)
- 12 findings, 1 LLM-realistic (false positive: missed `ret == NULL` guard)
- 0 real bugs (mature OSS-Fuzz target)

### curl — `lib/curlx/strparse.c` (2026-05-12)
- 20 leaf parsers, --real-libc --strict-dsl --raw-bytes default (NUL-terminated cursor)
- 8 verified clean (incl. `str_num_base`, `curlx_str_number`, `curlx_str_hex`,
  `curlx_str_quotedword`)
- 12 with CEs, 2 LLM-realistic (both false positives — over-strict harness
  postconditions misinterpreted by realism check)
- 0 real bugs

### curl — `lib/urlapi.c` (2026-05-12)
- 39 functions, --real-libc --strict-dsl, unwind=20
- 6 verified clean (`Curl_is_absolute_url`, `is_dot`, `host_decode`,
  `host_encode`, `allowed_in_path`, `set_url_port`)
- 16 with CEs (heavy `Curl_URL` opaque-struct manipulation)
- 17 errored at CBMC frontend
- 3 LLM-realistic findings, all false positives:
  - `curl_url_dup` :: pointer_dereference.1 — CBMC stub of `curlx_calloc`
    returns NULL; real code's `if(u)` guard handles this
  - `curl_url_cleanup` :: pointer_dereference.1 — same root cause
    (`Curl_ccalloc=NULL` is CBMC modelling of unset global)
  - `Curl_url_same_origin` :: pointer_dereference.79 — harness models
    a simplified version of the function, missing the
    `if(base->port && href->port)` guard
- 2 LLM-uncertain (genuinely uncertain, would need deeper
  cross-function analysis):
  - `guess_scheme` :: pointer_dereference.1 — `curlx_dyn_ptr(host)` can
    theoretically return NULL if `host` dynbuf was never written. Real
    caller chain (`parseurl → parse_authority → guess_scheme`) appears
    to guarantee population, but requires reading all parse_authority
    paths to confirm.
  - `hostname_check` :: strncmp.pointer_dereference.5 — embedded NUL in
    bracketed hostname passed to `ipv6_parse` could trigger string-fn
    OOB. Specific witness (hlen=2^60) unrealistic due to
    CURL_MAX_INPUT_LENGTH guard, but the bug class is plausible.
- 0 confirmed real bugs

### curl — `lib/parsedate.c` (2026-05-12)
- 17 functions after parser fix (commit 6217136), --real-libc --strict-dsl
- 9 verified clean (incl. `checkday`, `checkmonth`, `match_time`,
  `mktimet`, `time2epoch`, `tzadjust`, `datecheck`)
- 4 errored at CBMC frontend (`tzcompare` opaque `void*` qsort comparator,
  `parsedate` timed out at 120s, `curl_getdate`/`Curl_getdate_capped` opaque deps)
- 4 with CEs:
  - `checktz.pointer_dereference.1` — realism=unrealistic (CBMC bsearch
    stub returns symbolic pointer; real code has `if(what) return what->offset`
    NULL guard)
  - `datestring → checktz.pointer_dereference.1` — realism=realistic (BUT
    reasoning concludes false positive — same bsearch stub artifact;
    verdict label appears mismatched against reasoning)
  - `datenum.pointer.2` — realism=uncertain. The `date[-1]` read pattern is
    plausible UB *if* a caller passed `*datep` pointing to a different
    object than `indate`. Real curl always preserves the invariant
    (cursor advances from `indate` within the same input buffer).
    Witness doesn't actually trigger OOB.
  - `skip.unwind.0` — CBMC loop unwind exhausted, not a real bug
- 0 real bugs

### nghttp2 — `lib/nghttp2_hd_huffman.c` (2026-05-12)
- HPACK Huffman encoder/decoder, 5 functions
- 2 verified clean (`decode_context_init`, `decode_failure_state`)
- 1 timed out at 180s (`huff_decode` — needs higher CBMC timeout or
  loop-summarization to handle the giant 4980-line state table)
- 2 with CEs (`encode`, `encode_count`); after the `uint8_t *` raw-bytes
  fix, all flagged findings clustered around `nghttp2_bufs *` opaque
  struct manipulation
- 7 LLM-classified real-bug findings, but realism check called 2 of
  them unrealistic and the rest had no realism verdict (lower confidence)
- 0 confirmed real bugs

### libxml2 2.16.0 — full top-level sweep (2026-05-13, partial)
- Target: 39 top-level library `.c` files (~150k LoC), all features enabled
  (`--real-libc --strict-dsl --raw-bytes --enable-realism-check
  --enable-realism-thinking --enable-dynamic-validation
  --enable-flag-selection --threat-model security`), domain-knowledge file
  with libxml2-specific bug-class hints and CVE dedup list
- Run cancelled after ~4 hours when bmc-agent stalled on a hung Anthropic
  API call (process held two sockets open for ~35 min with no log progress);
  12 of 39 modules reached at least partial classification: HTMLparser (0
  classifications — frontend errored on most), HTMLtree (10/28), SAX2 (4/46),
  buf (31/44), c14n (29/40), catalog (42/77), chvalid (5/9), debugXML
  (29/40), dict (17/30), encoding (22/55), entities (10/21), error (4/27)
- 203 classifications total — 181 outcome=real_bug, 22 outcome=spurious
- Realism verdicts: 2 realistic, 25 uncertain, 154 unrealistic
- Both realistic findings triaged manually, **both false positives**:
  - `buf::xmlBufferEmpty pointer.1` — realism LLM claimed
    `xmlBufferDetach` only nulls `contentIO`, leaving `content` non-NULL.
    Source line 736 sets `buf->content = NULL` too, and xmlBufferEmpty
    line 803 short-circuits on `buf->content == NULL`. LLM hallucinated
    a missing guard.
  - `entities::xmlAddEntity pointer_dereference.47` — realism LLM claimed
    missing NULL check on `dtd->entities` sub-field. Source lines 246-250
    lazy-init the table (`if (dtd->entities == NULL) { dtd->entities =
    xmlHashCreateDict(...); }`). LLM also hallucinated a missing guard.
    Note: the LLM's response text actually said "verdict: UNCERTAIN" but
    bmc-agent's realism-parser stored it as realistic — parser quirk.
- 0 confirmed real bugs
- Cost: ~$? (key was provided inline; no separate cost tracking)
- Engineering observations:
  - bmc-agent has no client-side timeout on Anthropic API calls — if an
    API request hangs, the whole pipeline stalls indefinitely
  - HTMLparser.c was a frontend disaster — most functions errored with
    "too many addressed objects" (CBMC `--object-bits 8` default); needs
    `--object-bits 12` or per-function scope reduction for state-heavy
    files
  - Realism check on UNCERTAIN-class findings is the dominant cost; the
    real bottleneck for triage is LLM hallucinating missing guards that
    actually exist in source — repeat false positives suggest the realism
    prompt should require the LLM to quote the specific guard or its
    absence before voting REALISTIC

### libxml2 2.16.0 — `pattern.c` (2026-05-13, complete)
- 54 functions, all features enabled including `--enable-realism-check`,
  `--enable-dynamic-validation`, `--enable-flag-selection`, `--real-libc`,
  `--strict-dsl`, `--raw-bytes`. Domain knowledge file with libxml2-specific
  bug-class hints. Run took ~45 min wall-clock.
- 38 functions produced CBMC counterexamples (most multiple). 38 classifications.
- **0 realistic, 0 uncertain — 38 UNREALISTIC, all auto-rejected:**
  - 27 by the witness-pattern detector (library-init globals = NULL)
  - 3 by dynamic-validation-no-trigger
  - 2 by the LLM realism check
  - 6 by other paths (Phase 3 incomplete on a few)
- **Positive verification result**: pattern.c (XPath/XML pattern compiler +
  stream matcher) is free of bugs detectable by bmc-agent under the current
  model-artifact-rejection framework. Consistent with libxml2 being an
  OSS-Fuzz target — the engine's leaf parsers have well-formed bounds checks.
- bmc-agent improvements shipped during this sweep:
  - LLM client-side timeout (no more multi-hour sweep stalls)
  - Realism parser: JSON-key regex fix + conservative multi-keyword fallback
  - Realism max_tokens 2048 → 4096+ (no more JSON truncation)
  - Function body cap 2000 → 8000 chars in realism prompt
  - Full source-file context in realism prompt (LLM can cross-check claims)
  - Property-line `>>>` marker in source-file context
  - Source-location info from CBMC trace surfaced to realism prompt
  - Dynamic harness include-path propagation
  - CBMC --object-bits auto-scale on "too many addressed objects"
  - Allocator-family stub return contracts (malloc/calloc/realloc/strdup/getenv)
  - Self-referential struct pointer NULL-init (linked-list FP killer)
  - Parser typedef-alias resolution (separate-statement + `_Tag → Tag`)
  - Step 1.5 library-init assumptions in harness (prevents 27 FPs upstream
    in next sweep instead of rejecting them after CBMC)
  - Witness-pattern artifact detector (catches library-uninit witnesses
    before invoking realism LLM)
  - Feedback loop arm (b)+(c): LearnedConstraintsStore + LLM constraint
    distiller wired behind --enable-feedback-loop

## Confirmed real bugs

### jq 1.8.1 — `jvp_utf8_next` pointer-arithmetic UB (CWE-823)

- **Found**: 2026-05-12 (bmc-agent realistic verdict), confirmed
  2026-05-13 with AddressSanitizer.
- **bmc-agent commit at finding time**: e1df629 + this session's
  31 improvements.
- **File:line**: `src/jv_unicode.c:44`.
- **Property**: `pointer_arithmetic.17`
- **LLM realism verdict**: REALISTIC.
- **Witness**: 1-byte buffer with a 4-byte UTF-8 start byte (e.g. 0xF0).
- **Manual triage**:
  - Is the witness reachable through a public API? **YES** — every
    JSON parsing path that scans a string literal ending in a multi-byte
    UTF-8 start byte reaches this. `jvp_utf8_is_valid` calls
    `jvp_utf8_next` in a loop, exposed via `jv_string_check_utf8`.
  - Is there a precondition check the function relies on? Only
    `assert(in <= end)` — the check that fails (`in + length > end`)
    IS the guard, but it itself uses the UB pointer arithmetic.
  - Is there a NULL guard / bounds check upstream? Callers pass
    `(in, end)` sized buffers; no upstream prevents the boundary case.
- **CVE dedup**: no published CVE or GHSA matches `jv_unicode.c:44`.
  Issue #3483 (jv_parse.c:449 UB, fixed Feb 2026) shows jq triage
  accepts UBSan-class reports.
- **Bug class**: UB-pointer-arithmetic (CWE-823).
- **Severity estimate**: low-to-medium per practical impact (UB
  doesn't manifest at current -O2 on GCC/clang x86-64), but strictly
  UB by C11 standard. Future-compiler exploitable.
- **PoC**: confirmed with `gcc -fsanitize=address` +
  `ASAN_OPTIONS=detect_invalid_pointer_pairs=2`. PoC source at
  `/tmp/jq_ubsan_poc.c`. ASan reports
  `invalid-pointer-pair: ... 3 bytes after 1-byte region`.
- **Suggested fix**: replace `in + length > end` with
  `length > end - in`.
- **Full writeup**: `findings/bounty/jq_jvp_utf8_next_UB.md`.
- **Reported**: not yet — pending user decision to submit via
  jq private security-advisory channel.
