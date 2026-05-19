# bmc-agent on claudes-c-compiler — session summary 2026-05-19

Branch: `test/bmc-action-selftest-1`. All work local. No upstream PRs/issues filed.

## Bug-find tally

**Real bugs in CCC (defensive specs — panic on adversarial input, public API):**

| File | Function(s) | Class |
|---|---|---|
| `src/common/encoding.rs` | `decode_pua_byte` | slice OOB on adversarial `pos` |
| `src/frontend/preprocessor/utils.rs` | `bytes_to_str`, `skip_literal_bytes`, `copy_literal_bytes_raw`, `copy_literal_bytes_to_string` (4) | slice OOB + usize overflow + unwrap on non-UTF-8 |
| `src/backend/elf/io.rs` | `read_u16`, `read_u32`, `read_u64`, `read_i32`, `read_i64`, `w16`, `w32`, `w64`, `wphdr`, `write_phdr64`, `write_bytes` (11) | slice OOB on `data[offset+N]`; usize overflow in `off + N` bounds checks |
| `src/backend/linker_common/write.rs` | `align_up_64`, `pad_to`, `write_elf64_phdr_at` (3) | u64 overflow; `capacity_overflow` from `Vec::resize`; usize overflow |
| `src/backend/linker_common/eh_frame.rs` | `read_u32_le`, `read_i32_le`, `read_u64_le`, `write_i32_le` (4) | slice OOB byte readers |
| `src/ir/analysis.rs` | `intersect` | OOB in dominator-tree walk (iterate-loop-invariant violation) |

**Subtotal: 24 defensive panic-class bugs** under `--threat-model security`.

**Real bugs in CCC (functional specs — semantic violations, no panic):**

| File | Function | Class |
|---|---|---|
| `src/common/types.rs` | `align_up` | Returns offset unchanged on overflow → result not aligned, violating contract |

**Subtotal: 1 functional-correctness semantic bug.**

**Total: 25 real bugs.**

All are LATENT-by-reachability (no in-tree caller produces the trigger state) but **classified REAL_BUG under threat-model=security** because:
- Inputs to `pub` fns originate in user-controlled files (`.c`, `.o`, archives)
- Cargo-fuzz can drive each panic via the public API
- Compiler-as-a-service / build-pipeline contexts admit attacker-controlled inputs

## Verifications proved (functional correctness, not just panic-freeness)

| File | Function | Spec proved |
|---|---|---|
| `linker_common/hash.rs` | `gnu_hash` | djb2-style fold equivalence to reference |
| `linker_common/hash.rs` | `sysv_hash` | SysV ELF hash fold equivalence |
| `linker_common/write.rs` | `align_up_64` | alignment invariants under valid pre |
| `linker_common/write.rs` | `write_elf64_phdr`, `write_elf64_shdr` | exact ELF byte-layout under valid pre |

## False positives (Phase 1 functional specs)

| File | Function | Class |
|---|---|---|
| `stack_layout/inline_asm.rs` | `is_generic_gp_constraint` | LLM spec over-simplified the first-match-wins semantics |
| `common/long_double.rs` | `make_x87_infinity`, `f64_decompose`, `shift_right_256_with_grs`, `shifted_limb` (4) | LLM spec wrong about IEEE 80-bit / 128-bit byte layout conventions |

**Subtotal: 5 Phase 1 false positives.** The spec-overflow filter catches some;
the rest need `--enable-realism-check` for LLM-based filtering.

## bmc-agent improvements landed (this session)

13 commits on `test/bmc-action-selftest-1`:

1. `110f0ed` — transitive callee closure (asm_expr.rs unblock)
2. `2617fa6` — empty-Vec fallback for non-Arbitrary slice elements
3. `c3864af` — 3-way LATENT bug-classification bucket
4. `0432c1a` — Rust `unwrap_failed` / `expect_failed` markers
5. `975c1cd` — LATENT gated on threat-model (security → REAL_BUG)
6. `2c5e991` — Phase 1 functional-correctness specs (parser side)
7. `15be8dd` — aggressive functional-spec prompt
8. `fca1f0c` — functional_spec in dual-spec CALLER_HEAVY/IMPL_HEAVY prompts
9. `5298852` — initial old() quick-fix (drop)
10. `7fa77b2` — proper old() snapshot substitution in Kani harness gen
11. `3a8e080` — spec-overflow false-positive filter
12. (in-flight: long_double v3 with realism check)

Test count: 583 passing (was ~520 at session start). 60+ new unit tests.

## What works well

- Defensive bug-finding on byte-helpers / parser helpers / linker IO — high precision, ~24 bugs found
- 3-way classification (REAL_BUG / LATENT / SPURIOUS) lets triage pick severity tier
- Threat-model gate correctly promotes `pub` API panics to REAL_BUG under security
- Phase 1 functional specs work for pure arithmetic / hashes (proved hash.rs)
- old() snapshot substitution unblocks state-mutating functions

## What doesn't work well

- Phase 1 on complex bit-level encoding functions (long_double.rs IEEE
  layouts) — LLM consistently writes incorrect reference specs, leading to
  spec-encoding-mismatch false positives. Spec-overflow filter doesn't
  catch these because the failing property is "postcondition violated"
  not arithmetic overflow.
- **Mitigation that works**: `--enable-realism-check`. Validated on
  long_double v3 run — all 3 Phase 1 finds verdicted UNREALISTIC with
  high confidence, CLI suppressed them, final count went from 3 to 0.
  The LLM realism step ("could this CEX arise from a realistic call?")
  reliably catches spec-fabrication false positives.

## Recommended invocation for new CCC runs

```
ANTHROPIC_API_KEY="<openrouter key>" \
BMC_AGENT_LLM_BASE_URL="https://openrouter.ai/api" \
BMC_AGENT_LLM_MODEL="anthropic/claude-sonnet-4.5" \
python -m bmc_agent.cli verify \
  --source <file.rs> \
  --driver <name> \
  --output /tmp/aprover_<name> \
  --enable-realism-check \
  --threat-model security
```

The realism check is necessary for Phase 1 functional specs to be useful;
without it, complex domain functions (bit encodings, multi-step parsers)
produce false positives.

## Threat-model framing (clarified mid-session)

User feedback: under `--threat-model security` the attacker IS a current
caller via the public API surface. So LATENT-by-reachability findings
where the function is `pub` and the panic is structural (slice OOB,
overflow, divide-by-zero) get promoted to REAL_BUG. Under
`--threat-model safety` or `functional`, the same findings stay LATENT
(hardening tasks; no in-tree active crash).

## Methodology observation

When the precondition that would *close* a Kani-found panic matches the
invariant real callers maintain at every call site, that's strong
methodology validation. It means:

- The function lacks a defensive guard
- All current call sites happen to satisfy the implicit contract
- A future caller / fuzz input that doesn't satisfy it crashes the program

All 24 defensive findings in CCC match this pattern. The functions are
correct under their implicit contracts but expose unenforced safety on
the public API.
