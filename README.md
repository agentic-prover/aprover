<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <img alt="AProver" src="assets/logo.svg" height="80">
</picture>

**AProver — Agentic Prover for AI-Generated Code** — is a suite of LLM-driven formal verification agents. The first agent — **BMC-Agent** — is a prototype of *agentic model checking*: an architecture that pairs an LLM agent (for specification generation, counterexample classification, and spec refinement) with a sound bounded model checking backend. The agent handles semantic reasoning; the solver provides formal guarantees within the unwinding bound.

BMC-Agent supports two source languages with two solver backends, selected automatically by the source file's extension: **C** via CBMC, and **Rust** via Kani. The pipeline, classifier, refinement loop, and confidence tiers are shared; the parser and harness generator dispatch per language.

The design principle is *agents propose, conventional tools dispose*: every soundness-relevant decision the LLM produces passes through a conventional check (CBMC query, SMT soundness guard, or runtime confirmation) before affecting the verification verdict.

## How it works

```
Phase 1    Spec Generator        [AGENTIC]       LLM generates pre/postconditions top-down per function
Phase 2    BMC Engine            [CONVENTIONAL]  Checks each function against its spec via CBMC (C) or Kani (Rust)
Phase 3 S1 CEx Classifier        [AGENTIC]       LLM + CBMC: REAL_BUG / SPURIOUS / UNRESOLVED
Phase 3 S2 Feasibility check     [CONVENTIONAL]  Re-verifies violation with real callee bodies inlined
Phase 3 S3 Dynamic validation    [CONVENTIONAL]  Compiles + runs GCC harness to confirm fault at runtime
Phase 3 S4 Realism Checker       [AGENTIC]       LLM audits every REAL_BUG finding for realistic exploitability
Phase 3b   Spec Refiner          [AGENTIC]       Refines preconditions on spurious counterexamples
Phase 3c   Caller propagation    [CONVENTIONAL]  Re-verifies callers after spec refinement
Phase 4    Spec Quality          [AGENTIC]       Coverage / mutation / consistency checks (optional)
```

Each function is verified in isolation: callees are replaced with stubs constrained by their LLM-generated postconditions via `__CPROVER_assume`. The BMC backend then checks the function against its spec and those stubs. Cross-file callers are tracked via a two-pass global call-graph construction so that functions are not misclassified as system entry points.

Confirmed real bugs are assigned one of four evidence tiers:

| Tier | Meaning |
|---|---|
| `confirmed_dynamic` | Runtime fault (SIGSEGV/SIGABRT/SIGILL) directly observed in a GCC-compiled harness |
| `confirmed_system_entry` | Full call chain traced back to a function with no callers anywhere in the corpus |
| `confirmed_bmc` | Input reachability confirmed from at least one immediate caller via CBMC |
| `likely` | Over-refinement guard triggered |
| `unlikely` | Finding downgraded by the realism checker (verdict: UNREALISTIC) |

The **realism checker** (Phase 3 S4) runs an LLM audit on every `REAL_BUG` finding after dynamic validation. It asks whether real program execution could produce the counterexample's input state. Findings rated `UNREALISTIC` are downgraded to `unlikely` rather than discarded, preserving the full audit trail.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- [CBMC](https://github.com/diffblue/cbmc) on `PATH` (for C input)
- [Kani](https://github.com/model-checking/kani) on `PATH` (for Rust input; optional if you only verify C)
- An Anthropic API key (`ANTHROPIC_API_KEY`)

## Installation

```bash
git clone https://github.com/agentic-prover/aprover
cd aprover
uv sync
```

## Usage

```bash
export ANTHROPIC_API_KEY=your_key_here

# Verify a C source file (generate specs + run BMC + classify + refine)
uv run bmc-agent verify --source examples/simple_driver.c \
                        --driver simple_driver \
                        --output artifacts/

# Verify a Rust source file (backend dispatch is automatic by extension)
uv run bmc-agent verify --source path/to/your_module.rs \
                        --driver your_module \
                        --output artifacts/

# Verify a Rust file inside a real cargo workspace (multi-crate)
BMC_AGENT_KANI_REAL_CRATE=true uv run bmc-agent verify \
  --source path/to/crate/src/lib.rs \
  --driver crate_lib \
  --output artifacts/

# Generate specs only
uv run bmc-agent generate --source examples/simple_driver.c --driver simple_driver

# Verify every .c file in a directory — all validation tiers enabled
uv run bmc-agent verify-dir \
  --source-dir examples/vibeos/repo/kernel \
  --driver vibeos_kernel \
  --output artifacts/vibeos_kernel \
  --enable-dynamic-validation \
  --enable-realism-check \
  --enable-realism-thinking \
  --domain-knowledge "any domain notes for the LLM"

# CBMC-alone baseline (no LLM, no spec generation)
uv run bmc-agent baseline --source examples/simple_driver.c \
                          --driver simple_driver \
                          --output artifacts/baseline/

# Two-file cross-file demo (confirmed_system_entry across files)
uv run bmc-agent verify-dir \
  --source-dir examples/cross_file_demo \
  --driver cross_file_demo \
  --output artifacts/cross_file_demo
```

Artifacts — generated specs, CBMC harnesses, raw solver output, counterexample classifications, and bug reports — are written under `--output` and can be inspected or diffed.

### Web chat (no setup)

For a zero-configuration experience, `web/` contains a chat front-end that lets visitors run AProver by talking to it. The page streams pipeline progress (parse → spec → CBMC → classify → report) live as the agent works.

```bash
ANTHROPIC_API_KEY=sk-... uv run uvicorn web.server:app --port 7860
# open http://localhost:7860
```

Deploy as a Hugging Face Space with `web/deploy_to_space.sh` — see `web/README.md` for the full guide.

## Configuration

All settings are available as environment variables or `Config` dataclass fields.

| Variable | Default | Purpose |
|---|---|---|
| `BMC_AGENT_LLM_MODEL` | `claude-sonnet-4-6` | LLM model |
| `BMC_AGENT_CBMC_PATH` | `cbmc` | CBMC binary path |
| `BMC_AGENT_CBMC_UNWIND` | `4` | Loop unwinding bound |
| `BMC_AGENT_CBMC_TIMEOUT` | `120` | Solver timeout per function (seconds) |
| `BMC_AGENT_MAX_REFINEMENT_ITERS` | `5` | Maximum CEGAR refinement iterations |
| `BMC_AGENT_ENABLE_DUAL_SPEC` | `true` | Generate each spec twice, flag disagreements |
| `BMC_AGENT_ENABLE_SPEC_QUALITY` | `false` | Phase 4 spec-quality checks (mutation, coverage) |
| `BMC_AGENT_SKIP_REFINEMENT` | `false` | FilteringOnly mode: classify but don't refine |
| `BMC_AGENT_ENABLE_DYNAMIC_VALIDATION` | `false` | Phase 3 S3: compile + run a GCC harness |
| `BMC_AGENT_DYNAMIC_VALIDATION_TIMEOUT` | `30` | GCC harness run timeout (seconds) |
| `BMC_AGENT_DYNAMIC_CC_PATH` | `gcc` | C compiler for dynamic harness |
| `BMC_AGENT_ENABLE_REALISM_CHECK` | `false` | Phase 3 S4: LLM realism audit on every REAL_BUG finding |
| `BMC_AGENT_ENABLE_REALISM_THINKING` | `false` | Use extended thinking in the realism checker (higher quality, slower) |
| `BMC_AGENT_CBMC_UNSIGNED_OVERFLOW_CHECK` | `false` | Pass `--unsigned-overflow-check` to CBMC — detects integer overflow bugs (e.g. `calloc` `nmemb*size` wrap, CWE-190) |
| `BMC_AGENT_CBMC_REAL_LIBC` | `false` | Skip Python-side preprocessing and let CBMC see the real libc headers; required for sources that include `stdio.h` / `stdlib.h` directly (OpenSSL, libxml2, llm.c, …) |
| `BMC_AGENT_STRICT_DSL` | `false` | Forbid natural-language clauses in pre/post; pushes prose into the JSON `reasoning` field. Required for parser-state-heavy code where the LLM otherwise defaults to prose |
| `BMC_AGENT_RAW_BYTES` | `false` | Treat single `const char *` parameters as raw N-byte buffers (no NUL constraint). Required for wire-format readers that may read past `strlen` |
| `BMC_AGENT_INFER_FIELD_VALIDITY` | `false` | For struct-pointer parameters, init primitive-pointer fields (`float *`, `int *`, `double *`, …) as "NULL OR malloc'd backing buffer" instead of leaving them nondet. Prevents the harness from exploring "non-NULL but invalid" states that crash `memset(field, …)` despite a correct `if (field != NULL)` guard in source. Target audience: ML / numerics codebases (llm.c, ggml) whose struct fields are typed `float *` and aren't NUL-terminated strings |
| `BMC_AGENT_KANI_PATH` | `kani` | Kani binary path (Rust backend) |
| `BMC_AGENT_KANI_UNWIND` | `4` | Kani loop unwinding bound |
| `BMC_AGENT_KANI_TIMEOUT` | `120` | Kani solver timeout per harness (seconds) |
| `BMC_AGENT_KANI_SLICE_BOUND` | `4` | Bounded length used for `&[T]` slice / `Vec<T>` backing arrays in Kani harnesses |
| `BMC_AGENT_KANI_REAL_CRATE` | `false` | Run Kani via `cargo kani --tests --harness` inside the real crate root (multi-crate workspaces) instead of as a standalone `kani harness.rs` invocation |
| `BMC_AGENT_ENABLE_FEEDBACK_LOOP` | `false` | Enable the self-improvement loop: (a) developer code-changes, (b) function-spec invariant tightening, (c) project-wide invariant inference, with in-sweep iteration |
| `BMC_AGENT_ENABLE_FLAG_SELECTION` | `false` | Let the LLM select CBMC flags per function based on observed properties |

`BMC_AGENT_SKIP_REFINEMENT=true` is the FilteringOnly ablation: running the same input with and without this flag measures whether the refinement loop adds value beyond simple counterexample filtering.

## Specification DSL

Specs are expressed in a small DSL that is reliably emittable by an LLM and directly translatable to CBMC constructs without interpreter intervention.

| Predicate | Meaning | Translates to |
|---|---|---|
| `valid(ptr)` | Non-null pointer | `ptr != NULL` |
| `valid_string(ptr)` | Non-null, null-terminated C string | `ptr != NULL` + bounded buffer in harness |
| `valid_range(ptr, lo, hi)` | Non-null pointer, range `ptr[lo..hi)` in bounds | `ptr != NULL && lo >= 0 && hi >= lo` |
| `in_bounds(arr, idx)` | Array index in bounds | `idx >= 0 && idx < sizeof(arr)/sizeof(arr[0])` |
| `null(ptr)` | Null pointer | `ptr == NULL` |
| `owns(ptr)` | Caller-owned allocation | `ptr != NULL` |
| `locked(lock)` | Ghost lock state (skipped in harness) | `/* ghost */` |

Return values use `\result`. Arithmetic operators and C comparisons are translated directly. Natural language conditions that don't match any pattern are emitted as `/* comments */`.

## Evaluation — VibeOS

BMC-Agent was evaluated on [VibeOS](https://github.com/kaansenol5/VibeOS/tree/main), a bare-metal ARM64 hobby OS of ~15,000 lines written with substantial LLM assistance. Running `verify-dir` over all 37 kernel modules (675 functions) with all validation tiers enabled confirmed **13 realistic bugs** after the realism filter eliminated 48 unrealistic counterexamples.

| Function | Module | Tier | Signal | Root cause |
|---|---|---|---|---|
| `net_get_mac` | net.c | `confirmed_dynamic` | SIGSEGV | Null output pointer, no guard |
| `stbtt__buf_get` | ttf.c | `confirmed_dynamic` | SIGSEGV | CFF buffer OOB read (crafted font) |
| `stbtt__h_prefilter` | ttf.c | `confirmed_dynamic` | SIGABRT | Stack OOB write, user-controlled filter width |
| `stbtt_PackEnd` | ttf.c | `confirmed_dynamic` | SIGABRT | Double-free of font pack state |
| `hal_usb_keyboard_poll` | usb_hid.c | `confirmed_dynamic` | SIGSEGV | Null report buffer dereference |
| `vfs_lookup` | vfs.c | `confirmed_system_entry` | — | `parts[32]` stack overflow on deep path; `strtok_r` misuse on non-ASCII input |
| `vfs_open_handle` | vfs.c | `confirmed_system_entry` | — | `strcpy` overflow in path normalisation |
| `vfs_close_handle` | vfs.c | `confirmed_system_entry` | — | Use-after-free on node data |
| `strtok_r` | string.c | `confirmed_system_entry` | — | Tokenizer misuse |
| `hal_serial_getc` | serial.c | `confirmed_system_entry` | — | Null dereference in serial input |
| `align4` | dtb.c | `confirmed_system_entry` | — | Alignment violation in DTB parsing |
| `stbtt_GetPackedQuad` | ttf.c | `confirmed_system_entry` | — | Font atlas bounds (realism: uncertain) |
| `stbtt__csctx_rmove_to` | ttf.c | `confirmed_bmc` | — | CFF charstring NaN→int UB (CFF/OTF fonts only) |

The `calloc` integer overflow (CWE-190, `nmemb * size` wraps to zero) was confirmed in an earlier run and independently cross-validates [VibeOS issue #26](https://github.com/kaansenol5/VibeOS/issues/26).

## Examples

| File | Description |
|---|---|
| `simple_driver.c` | Ring-buffer device — off-by-one in `rb_write` |
| `sensor_hub.c` | CEGAR demo: spurious counterexample → refinement → real bug |
| `block_device.c` | Integer overflow in `blk_seek` |
| `memory_allocator.c` | Null dereference in `alloc_free` |
| `cross_file_demo/` | Cross-file `confirmed_system_entry`: null fn-pointer in `libmath.c` traced to `system_entry` in `main.c` |
| `vibeos/repo/kernel/` | Full VibeOS kernel — 13 confirmed realistic bugs (see table above) |

## Running tests

```bash
uv run pytest tests/ -q
```

## Project structure

```
bmc_agent/
  config.py             Configuration dataclass
  pipeline.py           End-to-end orchestrator
  spec_generator.py     Phase 1: LLM spec generation (top-down, caller-driven)
  bmc_engine.py         Phase 2: solver runner with parallel dispatch
  cex_validator.py      Phase 3 S1/S2: counterexample classification + feasibility check
  harness_generator.py  CBMC and GCC harness synthesis
  dsl_to_cbmc.py        Spec DSL → __CPROVER_assume / assert translation
  dynamic_validator.py  Phase 3 S3: GCC harness compile + run
  realism_checker.py    Phase 3 S4: LLM realism audit (REALISTIC / UNREALISTIC / UNCERTAIN)
  spec_quality.py       Phase 4: mutation testing, coverage, consistency checks
  feedback_loop.py      Optional self-improvement loop (arms a/b/c, in-sweep iteration)
  flag_selector.py      Optional per-function CBMC flag selection
  domain_analyzer.py    Domain-knowledge injection for spec generation
  preprocessor.py       Source preprocessing (cpp, real-libc bypass)
  bug_reporter.py       Confidence-tier classification + bug-report assembly
  parser.py             C parser (tree-sitter-c)
  rust_parser.py        Rust parser (tree-sitter-rust): free fns, impl methods, generics
  source_parser.py      Language-dispatch frontend
  kani.py               Kani invocation + result parsing
  cbmc.py               CBMC invocation + counterexample parsing
  llm.py                LLM client (Anthropic, OpenRouter-compatible providers)
  spec.py               Spec dataclass + serialization
  evaluation/           Baselines, metrics, corpus, report generation
  backends/             BMCBackend ABC, CBMCBackend, KaniBackend
examples/               Synthetic and real-world C / Rust targets
tests/                  Unit and integration tests
```

## Status

BMC-Agent is an active research prototype. The pipeline and all confidence tiers are stable.

**Working:**
- Full C verification pipeline via CBMC and full Rust verification pipeline via Kani; backend dispatch by source extension.
- Whole-codebase `verify-dir` mode; cross-file call-graph construction.
- Phase 3 S1 counterexample classification with structural downgrades (entry-fn postcondition, implicit-NULL precondition, structural-panic + over-refinement, path-divergent unwind).
- Phase 3 S2 feasibility check (real callee inlining), Phase 3 S3 dynamic validation (bare-metal-compatible GCC harness), Phase 3 S4 realism checker (LLM audit with optional extended thinking and witness-pattern auto-rejection).
- Five-tier confidence reporting (`confirmed_dynamic` / `confirmed_system_entry` / `confirmed_bmc` / `likely` / `unlikely`).
- CEGAR spec refinement loop, callee stub postcondition constraints, FilteringOnly ablation (`--skip-refinement`), domain knowledge injection (`--domain-knowledge`), spec-quality module, prompt caching.
- Wire-format C support (`--real-libc`, `--strict-dsl`, `--raw-bytes`, paired-pointer detection, `T**` in-out cursor pattern) — validated on OpenSSL ASN.1, libxml2, jq, protobuf upb.
- Rust harness generator supports primitives, raw pointers, `&T`, `&[T]` slices, `Vec<T>`, `Option<T>`, `&str`, tuple returns, generics with monomorphisation, `where` clauses, `unsafe fn`, pointer-newtype detection (`SendPtr { ptr: *mut T }`); cargo-mode for multi-crate workspaces.
- Self-improvement feedback loop (`--enable-feedback-loop`) with three arms (developer code-changes, function-spec invariant tightening, project-wide invariant inference) and in-sweep iteration.

**Partial / planned:** spec-quality analysis at scale; evaluation corpus beyond VibeOS; manual precision audit of sampled findings; constructor-pattern precondition inference (planned for ML-kernel targets); loop-invariant generation in spec gen.

## License

4-clause BSD. See [LICENSE](LICENSE).
