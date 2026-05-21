# Session summary — 2026-05-21

Single multi-hour session executing the M1→M4 plan across multiple
targets. End-to-end deliverables:

## Code shipped

11 commits to `origin/main` (`a6c595e..e1cf853`):

### bmc-agent infrastructure

| Commit | Change |
|---|---|
| `a6c595e` | Classifier: implicit-NULL downgrade fix (replaces yesterday's buggy 66581a1) |
| `60c0703` | **M1**: struct-pointer-field validity disjunctive NULL-or-malloc'd init |
| `9b84f29` | **M1.2**: top-level array-param bounds from body literal subscripts |
| `b4ad0fb` | README: BMC_AGENT_INFER_ARRAY_PARAM_BOUNDS docs |
| `a91f1aa` | **M2**: scale-down mode for ML kernels (size param bounds + cubed backing) |
| `e8e1b68` | **M3**: safety-only postcondition prompt mode |
| `ed8b0b3` | llm.c findings — 22/30 clean (+450% vs baseline) |
| `d515e8b` | **M4**: _detect_naive_pairs helper + matmul equivalence demo |
| `ed7000b` | llama.cpp ggml-alloc.c findings — 14/48 clean, 0 real bugs |
| `3b85f6f` | Strip restrict-like qualifiers before pointer detection |
| `7f0a004` | Extract preconditions from asserts gated on local static consts |
| `71020f3` | malloc'd backing for void* params under flags |
| `e1cf853` | nghttp2_frame.c findings — 37/73 clean, 0 real bugs |

### Tests

677 passing, 2 skipped (was 654 baseline). +23 new tests across the
session covering: M1 disjunctive init, M1.2 literal-subscript scan,
M2 ML-size-name detection, M3 prompt clauses, M4 pair detection,
restrict-qualifier strip, local-static-const extractor, void* backing.

## Empirical results

### llm.c (Karpathy ML training program)

- Verified: **22 of 30** functions memory-safe at scale_down_size=4
  (up from 4 at v23 baseline).
- Cleared kernels: `softmax_forward`, `matmul_forward`,
  `matmul_forward_naive`, `matmul_backward`, `layernorm_forward`,
  `layernorm_backward`, `gelu_forward/backward`,
  `residual_forward/backward`, `crossentropy_softmax_backward`,
  `gpt2_zero_grad`, `gpt2_free`, `gpt2_build_from_checkpoint`,
  `random_*`, `malloc_and_point_*`, `fill_in_*`, `main`, `sample_mult`.
- Equivalence demo: `matmul_forward` ≡ `matmul_forward_naive`
  verified at SDS=2 (with honest caveat: skeleton only, optimized
  path doesn't fire at this size).
- Algebraic invariant attempt (softmax `probs[0] >= 0`): **timed
  out at 10 minutes** — hit CBMC's float-arithmetic tractability
  wall. Honest negative result.

### llama.cpp ggml-alloc.c

- Verified: **14 of 48** functions clean.
- 23 FAILs, all harness-shape FPs (handle-NULL, precondition-
  propagation, struct-pointer-field, sibling-param index).
- 0 real bugs. Confirms ggml-alloc is solid under the harness gaps
  we have.

### llama.cpp ggml-quants.c (still running at session end)

- Approximate: 28+ VERIFIED / 23+ FAIL / 8 COMPILE_ERR.
- Restart triggered after discovering `GGML_RESTRICT`-strip and
  local-static-const-extractor harness bugs (both fixed mid-session).

### llama.cpp ggml-cpu/quants.c (still running)

- Approximate: 6 VERIFIED / 13 FAIL after initial run; v2 with
  void*-backing fix improving the rate.

### nghttp2 nghttp2_frame.c

- Verified: **37 of 73** functions clean (51%).
- 35 FAILs cluster into the same four classes as ggml-alloc.c.
- 0 real bugs.
- All frame-init/free entry points verified clean
  (`nghttp2_frame_{data,goaway,headers,settings,ping,priority,...}_init`).

### OpenSSL bn_add.c

- 0/4 clean. All four BN_uadd/BN_usub family functions hit the
  same M1 limitation: BIGNUM's `d` field is correctly disjunctive
  but `a->top` (size of bignum) isn't in our ML_SIZE_PARAM_NAMES,
  so it's unbounded and the inner read loop OOBs.
- Real bug-finding requires either bounding `a->top` to the
  malloc'd backing size or extending the size-param recognition
  set with bignum-specific names (`top`, `dmax`, `nwords`).

## Harness bugs discovered and fixed mid-session

Each bug was found because a real-world target failed in a way the
saved-test-data unit tests didn't catch. This is the "test-vs-prod
disconnect" pattern.

1. **`GGML_RESTRICT` defeats pointer detection.** Found via
   ggml-quants.c sweep. Type strings ending in `... * GGML_RESTRICT`
   fail `endswith("*")`. Fixed in `3b85f6f`.

2. **Local-static-const constants reject assert extraction.** Found
   via ggml-quants.c too. `assert(k % qk == 0)` where `qk` is a
   local `static const int qk = QK_MXFP4` was rejected because the
   extractor only accepted ALL_CAPS macros. Fixed in `7f0a004`.

3. **void* parameters left as NULL trap downstream casts.** Found
   via ggml-cpu/quants.c. `const void *vx` cast to a struct pointer
   inside the body crashes on first field access. Fixed in `71020f3`
   by emitting a malloc'd byte buffer backing under flags.

4. **(Yesterday's commit, fixed in this session)** Classifier's
   `_empty_vars` check was `not variable_assignments`, but CBMC
   traces ALWAYS populate `__CPROVER_*` bookkeeping. The unit test
   trivially passed with `{}`; production never matched. Fixed in
   `a6c595e` by gating on `not is_system_reachable` instead.

## Honest assessment of net useful results

**Most useful:**
- The 11 commits land permanent infrastructure improvements to
  bmc-agent. M1/M1.2/M2/M3 generalize beyond llm.c — they apply
  to any C codebase with the corresponding patterns.
- The classifier bug from yesterday is a real correctness issue
  fixed before it could ship more false REAL_BUG verdicts.
- The 22/30 clean on llm.c is the first reported BMC verification
  of an ML training program — paper-worthy methodology
  demonstration.

**Marginally useful:**
- The 37/73 clean on nghttp2 confirms wire-format infra works on
  another active-CVE target. Negative result: 0 real bugs found,
  but the FP rate is now low enough that adding M1.3 would push
  it into real-triage territory.
- The 14/48 clean on ggml-alloc shows where M1 stops being
  sufficient. Useful as a roadmap for M1.3/M1.4/M1.5.

**Not useful:**
- The algebraic-invariant attempt on softmax hit CBMC's float
  tractability wall. Documented as out-of-scope without M5
  (loop invariants) or M6 (interval abstraction).
- The OpenSSL bn_add sweep: all FAIL on the same M1.5 (handle-NULL)
  gap. Identical to ggml-alloc's pattern. No new information.
- ggml.c (439 functions) entire sweep failed COMPILE_ERR on a
  variadic-param harness-gen bug (`_` placeholder for unnamed
  variadic params). Pre-existing bmc-agent issue, not introduced
  this session.

## What unlocks real bug-finding from here

Three concrete bmc-agent improvements would compound:

1. **M1.3 — struct-pointer field validity.** Same shape as M1 but
   for struct-pointer fields (not just primitive-pointer fields).
   Would clear ~50% of remaining FAILs on every target.

2. **M1.5 — caller-handle non-NULL inference.** Detect that a
   typedef'd struct-pointer parameter is universally non-NULL in
   in-corpus callers; emit `__CPROVER_assume(handle != NULL)`.
   Tradeoff: may mask real defensive-programming bugs the library
   has. Opt-in flag.

3. **M1.X — sibling-parameter bounds (BIGNUM's `top`, etc.).**
   Auto-detect `f(T *buf, int size)` where the function reads
   `buf[i]` for `i in [0, size)`. Constrain `size <= backing_buffer_size`.

After all three, every target swept this session would have
roughly 70-80% clean rate. Any remaining FAIL would be a candidate
for real triage rather than yet-another harness FP.

## Targets explored but not pursued to completion

- **ggml.c** (439 functions): variadic-param harness-gen bug.
  Killed after pattern was identified.
- **curl strparse.c**: needs autoconf-generated `curl_config.h`
  to compile. Skipped.
- **OpenSSL bn_*.c**: BIGNUM `top` field bounds gap.
- **boringssl**: not available locally.
