# False-positive reflections → bmc-agent improvements

When a REALISTIC finding turns out to be a false positive after manual
triage, record the FP pattern here and what bmc-agent change would have
caught it. Convert each entry into a code change before the next sweep.

Entry format:
```
### YYYY-MM-DD — target::function — failing-property
- FP-class: <pattern category>
- What the LLM/CBMC got wrong: <one paragraph>
- Source-of-truth that contradicted: <file:line>
- bmc-agent change(s) shipped: <commit / file:line>
```

---

### 2026-05-13 — libxml2::xmlBufferEmpty — pointer.1
- FP-class: realism LLM hallucinated missing guard
- What it got wrong: claimed `xmlBufferDetach` only nulls `contentIO`,
  leaving `content` non-NULL, so `xmlBufferEmpty` would enter the
  XML_BUFFER_ALLOC_IO branch with `content != NULL` && `contentIO ==
  NULL` and deref through NULL.
- Source-of-truth: `buf.c:735-736` — xmlBufferDetach sets BOTH content
  AND contentIO to NULL. `buf.c:803` short-circuits on `content == NULL`.
- bmc-agent change shipped:
  - `realism_checker.py:_format_source_file_context` — full source file
    body sent in realism prompt so the LLM can cross-check claims about
    other functions in the same file.
  - `prompts.py:REALISM_CHECK_PROMPT` REQ-2 — explicit cross-reference
    rule: "for every function you name in the chain, that function MUST
    appear in the FULL SOURCE FILE CONTEXT".

### 2026-05-13 — libxml2::xmlAddEntity — pointer_dereference.47
- FP-class: function-body truncation hid the guard the LLM denied
- What it got wrong: claimed "no guard found" for `dtd->entities` access,
  proposing missing-NULL-check as bug class.
- Source-of-truth: `entities.c:246-250` — explicit lazy-init
  `if (dtd->entities == NULL) { dtd->entities = xmlHashCreateDict(...) }`
  was at char 2700 of the function body; realism prompt truncated body
  at char 2000.
- bmc-agent change shipped:
  - `realism_checker.py:_build_prompt` — function_body cap raised
    2000 → 8000 chars.
  - Full source file context (above) ALSO covers this.

### 2026-05-13 — libxml2::xmlPatternStreamable — pointer_dereference.1 (anticipated)
- FP-class: CBMC linked-list traversal artifact
- What it got wrong (anticipated, realism not yet done): CBMC treated
  `comp->next` (a self-referential pointer field) as symbolic-nondet.
  On iteration N+1, `comp = comp->next` resolves to a non-NULL but
  garbage pointer; `comp->stream` is an OOB read. Real chains are
  always NULL-terminated by xmlPatterncompile.
- Source-of-truth: `pattern.c:2279-2287` — function explicitly guards
  `if (comp == NULL) return(-1)` and the `while (comp != NULL)` loop;
  the chain is built by xmlPatterncompile via a singly-linked-list
  pattern that always terminates.
- bmc-agent change shipped:
  - `harness_generator.py:_emit_struct_field_init` — when a struct
    field's pointee type matches the enclosing struct (self-referential
    linked-list pointer), force the field to NULL at harness construction.
  - `harness_generator.py:_matches_struct_tag` — normalizes ``struct
    _xmlPattern`` vs ``xmlPattern`` typedef so the match works.

### Class: CBMC witness with library-init globals = NULL
- FP-class: CBMC default state requires library to be uninitialized
- What it got wrong: many witnesses set `xmlMalloc = NULL`,
  `xmlFree = NULL`, `xmlRealloc = NULL` simultaneously. CBMC's default
  for unset globals is NULL; real public-API call chains all go through
  ``xmlInit``/``curl_global_init``/``OPENSSL_init_crypto`` at startup
  which assigns these.
- bmc-agent change shipped (v1, post-hoc reactive):
  - `realism_checker.py:_witness_indicates_uninitialized_library` —
    detects ≥2 library-init global function pointers = NULL in the
    witness; short-circuits to UNREALISTIC before the LLM call. Covers
    libxml2 / libcurl / OpenSSL / glib allocator pointers.
  - Test: `test_realism_checker.py::test_witness_uninitialized_library_*`.
- bmc-agent change shipped (v2, proactive prevention):
  - `harness_generator.py:_emit_library_init_assumptions` — emits
    `__CPROVER_assume(xmlMalloc != NULL); …` at harness Step 1.5
    so CBMC never explores the impossible "library uninitialized"
    state-space. Detects which globals are referenced in the parsed
    file before emitting (no spurious link errors).
  - Saves CBMC time AND eliminates the FP entirely instead of
    rejecting it after generation. This is the right evolution:
    catch model artifacts at the constraint level, not at triage.

### Class: Parser misses separate-typedef `_Tag → Tag` aliases
- FP-class: harness-generator path that emits per-field struct init
  with self-ref pointer NULL never triggered because typedef alias
  ``xmlPattern`` wasn't resolved to ``struct _xmlPattern { … }``;
  pattern.c declares the typedef in a separate header.
- bmc-agent change shipped:
  - `parser.py:_collect_struct_defs` — handles separate
    `typedef struct Tag Alias;` statements and the libxml2 idiom of
    leading-underscore tags (`_xmlPattern` → `xmlPattern`).
  - Tests: `test_phase2.py::test_parser_resolves_separate_typedef_alias`,
    `test_parser_resolves_underscore_tag_convention`.

### Class: CBMC --object-bits 8 ceiling on state-heavy files
- FP-class: CBMC "too many addressed objects" frontend errors hid real
  signal on parser.c / HTMLparser.c (>256 distinct objects allocated).
- bmc-agent change shipped:
  - `cbmc.py:run_cbmc` — auto-retries at `--object-bits 12` then 16
    when CBMC reports the 2^n ceiling.
  - `config.py` — `cbmc_object_bits` / `cbmc_auto_scale_object_bits`.

### Class: Allocator stub return = arbitrary garbage pointer
- FP-class: stubbed malloc/realloc/strdup return unconstrained pointers
  that alias unrelated memory; harness then "dereferences" garbage and
  reports OOB / NULL deref.
- bmc-agent change shipped:
  - `harness_generator.py:_builtin_stub_return_contract` —
    __CPROVER_assume on result for malloc / calloc / realloc / strdup
    / strndup / getenv variants (libc + libxml2 + libcurl + OpenSSL +
    glib). Forces `result == NULL || __CPROVER_w_ok(result, size)`.

### Class: LLM API hang stalls multi-hour sweep
- FP-class: bmc-agent had no client-side timeout on Anthropic SDK
  calls; a hung connection blocked the whole pipeline for 35+ minutes
  on the libxml2 full sweep.
- bmc-agent change shipped:
  - `llm.py` — applies `client.with_options(timeout=N)` per request.
  - `config.py` — `llm_request_timeout_s` (default 180s).

### 2026-05-13 — jq::jv_alloc.c (jv_mem_calloc, jv_mem_calloc_unguarded) — *.assertion.1
- FP-class: internal-helper source-level `assert(precondition)` fires
  when harness passes nondet parameters
- What it got wrong: both functions open with `assert(nemb > 0 && sz > 0);`
  documenting an internal-only precondition. CBMC's nondet `(nemb, sz)`
  trivially violates it. Realism check ALSO failed this sweep because
  the run used `/usr/bin/python3` (no anthropic SDK installed); the
  finding fell through to `cex_outcome=real_bug`.
- Source-of-truth: `jv_alloc.c:154`, `jv_alloc.c:163` — the asserts
  themselves are the precondition spec. All real callers (`jv.c:jv_array`,
  `jv.c:jv_string_sized`, …) pass non-zero values.
- bmc-agent change shipped:
  - `harness_generator.py:_extract_source_precondition_asserts` —
    parses function body for top-of-function `assert(expr)` over
    parameters and auto-emits `__CPROVER_assume(expr)` at Step 1.8 of
    the harness (before Step 2 spec preconditions).
  - Both real-libc and stubbed-callee harness paths wired up.
  - Conservative: rejects `assert(0)` / `assert(false)` (unreachability
    markers), rejects asserts mentioning globals (non-parameter
    identifiers), stops at first non-assert statement (asserts after
    state mutation might not be pure preconditions).
  - Tests: 5 new in `test_phase2.py::test_source_assert_*`.

### 2026-05-13 — jq::jv_aux.c (parse_slice, jv_get, jv_has, jv_getpath, jv_group, jv_sort, jv_unique, sort_items) — multiple
- FP-class: jq jv tagged-union stub-disconnect
- What it got wrong: jv is a refcnt-backed tagged-union struct
  `{ kind_flags, pad_, offset, size, u: { ptr | number } }`. The
  harness declared `jv j;` (nondet zero-bytes), making `j.u.ptr == NULL`
  and `j.kind_flags == 0`. The stubbed `jv_get_kind` then returned
  unconstrained nondet enum values — including JV_KIND_ARRAY,
  JV_KIND_STRING, JV_KIND_OBJECT, or out-of-range integers (17, 518,
  2097152, 268435463). The implementation under test then dereferenced
  the NULL refcnt, segfaulting. Real jq code obtains jv values only via
  constructors (jv_array, jv_string, jv_object, …) which always pair
  refcnt-backed kinds with valid heap-allocated refcnt.
- Source-of-truth: `jv.c` constructors (jv_array, jv_string, jv_object,
  jv_number) — every refcnt-backed jv kind comes with a non-NULL
  `u.ptr`. `jv_get_kind()` reads `jv->kind_flags & 0x0F` so a zero
  struct returns JV_KIND_INVALID (0), not a refcnt-backed kind.
- bmc-agent change shipped:
  - `realism_checker.py:_witness_indicates_jv_stub_disconnect` —
    auto-rejects CEs where ≥1 `*.u.ptr` is NULL AND ≥1 stubbed
    `jv_get_kind` returns a refcnt-backed kind (STRING/ARRAY/OBJECT/
    NUMBER) or out-of-enum-range integer.
  - `cex_validator.py:_witness_obvious_artifact` — pre-classifier
    filter calls the new detector to skip the LLM reachability call.
  - Tests: `test_realism_checker.py::test_jv_stub_disconnect_*` (3 new).
- Validation: applied to all 25 jv_aux.c "confirmed bugs" → all 8 with
  real CE data flagged as artifact. The other 17 were pipeline-recorded
  shells (CBMC parse error, no actual CE).

### 2026-05-13 — libxml2::xmlAddEntity — verdict parsing
- FP-class: parser bug, LLM said UNCERTAIN but stored realistic
- What it got wrong: max_tokens=2048 truncated the JSON; parser fell
  back to `_recover_verdict_from_prose`; regex
  `VERDICT\s*[:\-=]?\s*"?(...)` didn't match the JSON-key form
  `"verdict": "UNCERTAIN"`; fell through to bare-keyword search which
  hit the word "realistic" in the reasoning.
- Source-of-truth: the LLM literally wrote `"verdict": "UNCERTAIN"`.
- bmc-agent change shipped:
  - `realism_checker.py:_recover_verdict_from_prose` — new regex
    handles `"VERDICT": "X"` JSON-key form first; conservative
    multi-keyword fallback defaults to UNCERTAIN.
  - `realism_checker.py:check()` — max_tokens raised 2048 → 4096
    (or 4096 + 4000 thinking) to prevent truncation.
  - `tests/test_realism_checker.py` — regression test added.
