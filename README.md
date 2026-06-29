<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <img alt="AProver" src="assets/logo.svg" height="80">
</picture>

### 🌐 [**Try AProver live → www.aprover.ai**](https://www.aprover.ai)

Point the **workbench** at a public repo (or a subdirectory / single file), choose a scope, and watch BMC-Agent generate specs, run CBMC, classify counterexamples, and report confirmed bugs with evidence tiers — with live token/$ spend, pause, and granular recovery — right in your browser, no install. Bring your own API key (it stays in your browser).

---

**AProver — Agentic Prover for AI-Generated Code** — is a suite of LLM-driven formal verification agents. The first agent — **BMC-Agent** — is a prototype of *agentic model checking*: an architecture that pairs an LLM agent (for specification generation, counterexample classification, and spec refinement) with a sound bounded model checking backend. The agent handles semantic reasoning; the solver provides formal guarantees within the unwinding bound.

> 📄 **Paper:** [*Agentic Model Checking*](https://arxiv.org/abs/2605.21434) — Youcheng Sun, Jiawen Liu, Daniel Kroening, Jason Xue (arXiv:2605.21434, 2026). This is the reference for the ideas implemented here; please [cite it](#citation) if you use AProver / BMC-Agent in your work.

BMC-Agent supports three source languages with three solver backends, selected automatically by the source file's extension: **C** via CBMC, **Rust** via Kani, and **Java** via JBMC. The pipeline, classifier, refinement loop, and confidence tiers are shared; the parser and harness generator dispatch per language. (Java/JBMC currently runs as whole-program verification; the agentic per-function spec pipeline applies to C and Rust.)

The design principle is *agents propose, conventional tools dispose*: every soundness-relevant decision the LLM produces passes through a conventional check (CBMC query, SMT soundness guard, or runtime confirmation) before affecting the verification verdict.

The same architecture runs in reverse as a **specification synthesizer**: given a program annotated with assertions, it proposes function contracts and loop invariants and discharges them with a deductive oracle — CBMC for bounded checks, or Frama-C/WP for unbounded loops and quantified/overflow-rigorous contracts. See [Specification synthesis](#specification-synthesis).

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
- [JBMC](https://github.com/diffblue/cbmc) + a JDK (`javac`) on `PATH` (for Java input; optional otherwise)
- [Frama-C](https://frama-c.com/) + an SMT prover (e.g. Alt-Ergo) on `PATH` (optional; only for `--oracle frama-c` specification synthesis)

> The Nix devshell and the `aprover-web` service provision all of these
> automatically — including JBMC and Kani, which are built from source (nixpkgs
> ships CBMC with `-DWITH_JBMC=OFF` and does not package Kani). See
> [`nix/tools.nix`](nix/tools.nix). Outside Nix, install the tools yourself.
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

# Route spec-gen + refinement through your local Claude Code login (no API key);
# add --claude-code-agentic to let it read the source tree (Read/Grep/Glob), or
# --provider claude-code to route every role through the CLI.
uv run bmc-agent verify --source examples/simple_driver.c --driver simple_driver \
                        --specs-via-claude-code

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

### Agentic mode and post-hoc components

`--agentic` makes every LLM step an investigating agent (Claude Code by default;
re-point any one with `BMC_AGENT_LLM_<ROLE>_PROVIDER`), while the conventional core
(tree-sitter parse, CBMC, deterministic harness translation, compile+run) stays
conventional. Under `--agentic` the **classifier + spurious→refinement→soundness-gate
loop stays on** (the sound core) and the **dynamic reproducer is on**; only the noisy
LLM judgment layers — **realism** (exploitability downgrade) and **triage** (severity
tiering) — are **off by default** and independently opt-in:

```bash
# spec-gen + refinement + soundness gate + harness-repair + classifier are agentic/on;
# realism + triage are OFF; the dynamic reproducer confirms.
uv run bmc-agent verify-dir --source-dir SRC --driver d --output OUT --agentic
# opt a judgment layer back in (independent of each other):
#   --enable-realism-check   --enable-triage
# lean batch variant (spec-gen stays on the fast default LLM): --agentic-refine
```

The classifier, realism and triage components also run **independently on a
finished run's artifacts** (`<output>/<driver>/<fn>/…`), so you can apply them
after the fact (and pick the backend per the same role env vars):

| Component | Post-hoc tool |
|---|---|
| Classifier / judge | `uv run bmc-agent judge-dir --report-dir OUT …` |
| Realism audit | `uv run python scripts/rerun_realism.py …` |
| Triage (UNRESOLVED CExs) | `uv run python scripts/triage_unresolved.py --sweep-dir OUT --driver d …` |

## Specification synthesis

Instead of hunting bugs, BMC-Agent can synthesize the **specifications** that make a program's assertions provable. Given a file whose goals are written as `//@ assert`, `assert(...)`, `static_assert`, or `__VERIFIER_assert`, the `--specs-bench` preset auto-dispatches by program content and emits ACSL:

- **no loops → function-contract synthesis** — propose a postcondition, check it makes the asserts hold (sufficiency) and is implied by the body (soundness), then strengthen toward the exact input/output relation.
- **loops → loop-invariant synthesis** — propose inductive invariants (scalar, quantified-array, or recursive-fold) sufficient to discharge the goals.

```bash
# Synthesize + verify with the Frama-C/WP deductive oracle
uv run bmc-agent verify --source prog.c --driver prog.c --agentic --specs-bench --oracle frama-c
```

`--specs-bench` turns on `--math-ints` (the mathematical-integer semantics these benchmarks assume). Overflow-rigor is on by default: a math-int result is re-verified with RTE on and reported as either machine-int **sound** or **math-int only** when the body genuinely overflows (`--no-overflow-rigor` to opt out). `--oracle cbmc` (the default off-preset) handles bounded loops; `--oracle frama-c` handles unbounded loops and quantified/overflow contracts.

## Configuration

All settings are available as environment variables or `Config` dataclass fields.

| Variable | Default | Purpose |
|---|---|---|
| `BMC_AGENT_LLM_MODEL` | `claude-sonnet-4-6` | LLM model |
| `BMC_AGENT_LLM_PROVIDER` | _(auto)_ | LLM provider for all roles: `anthropic`, `openai` (K2 / OpenAI-compatible), or `claude-code` (local `claude` CLI, no API key). Empty = auto-detect. Per-role override: `BMC_AGENT_LLM_<ROLE>_PROVIDER` for `SPEC_GEN`/`REFINEMENT`/`REALISM`/… (CLI sugar: `--provider`, `--specs-via-claude-code`) |
| `BMC_AGENT_CLAUDE_CODE_BIN` | `claude` | Path to the Claude Code CLI (used only when provider is `claude-code`) |
| `BMC_AGENT_CLAUDE_CODE_TIMEOUT_S` | `600` | Per-call timeout for the `claude -p` path (seconds) |
| `BMC_AGENT_CLAUDE_CODE_AGENTIC` | `false` | Let the `claude-code` provider use read-only tools (`Read`/`Grep`/`Glob`) to explore the source tree while drafting/refining specs, instead of a one-shot text completion (CLI: `--claude-code-agentic`) |
| `BMC_AGENT_CLAUDE_CODE_TOOLS` | `Read,Grep,Glob` | Tool allowlist for agentic claude-code mode (keep read-only) |
| `BMC_AGENT_CBMC_PATH` | `cbmc` | CBMC binary path |
| `BMC_AGENT_CBMC_UNWIND` | `4` | Loop unwinding bound |
| `BMC_AGENT_CBMC_TIMEOUT` | `120` | Solver timeout per function (seconds) |
| `BMC_AGENT_JBMC_PATH` | `jbmc` | JBMC binary path (Java backend) |
| `BMC_AGENT_JAVAC_PATH` | `javac` | `javac` path used to compile Java sources before JBMC |
| `BMC_AGENT_JBMC_UNWIND` | `4` | JBMC loop unwinding bound (falls back to `BMC_AGENT_CBMC_UNWIND`) |
| `BMC_AGENT_JBMC_TIMEOUT` | `120` | JBMC solver timeout (seconds; falls back to `BMC_AGENT_CBMC_TIMEOUT`) |
| `BMC_AGENT_JAVA_COMPILE_TIMEOUT` | `60` | `javac` compile timeout (seconds) |
| `BMC_AGENT_JAVA_CLASSPATH` | `` | Extra classpath entries (os.pathsep-separated) for the Java compile/verify |
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
| `BMC_AGENT_INFER_FIELD_VALIDITY` | `false` | Init struct primitive-pointer fields (`float *`, …) as "NULL or malloc'd buffer" so a correct `if (field != NULL)` guard isn't defeated by nondet-invalid states (ML structs) |
| `BMC_AGENT_INFER_ARRAY_PARAM_BOUNDS` | `false` | Size a pointer parameter's harness backing array from the max literal subscript in the body (cap `…_MAX`, default 64), not 1 element |
| `BMC_AGENT_SCALE_DOWN` | `false` | ML-kernel scale-down: bound parametric sizes (`B`, `T`, `C`, `NH`, …) to `[0, …_SIZE]` (default 4) and auto-enable array-param bounds; stops matmul/attention timeouts |
| `BMC_AGENT_SAFETY_ONLY` | `false` | Restrict postconditions to memory safety + range + NaN/Inf-freedom (no functional claims); pairs with `SCALE_DOWN` for ML kernels |
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

## Evaluation

**VibeOS** ([repo](https://github.com/kaansenol5/VibeOS/tree/main)) — a ~15,000-line bare-metal ARM64 hobby OS written with substantial LLM assistance. `verify-dir` over all 37 kernel modules (675 functions), every tier on, confirmed **13 realistic bugs** (after the realism filter dropped 48 unrealistic counterexamples) — dynamically-reproduced crashes (`net_get_mac` null deref, `stbtt__h_prefilter` stack OOB write, `stbtt_PackEnd` double-free) and `confirmed_system_entry` flaws (`vfs_lookup` deep-path stack overflow, `vfs_open_handle` `strcpy` overflow, `vfs_close_handle` use-after-free). A separate `calloc` integer overflow (CWE-190) cross-validates [VibeOS issue #26](https://github.com/kaansenol5/VibeOS/issues/26).

**llm.c** ([repo](https://github.com/karpathy/llm.c)) — Karpathy's `train_gpt2.c`, the full GPT-2 forward+backward pass (~1100 lines, no LLM assistance). With the M1–M2 milestones (`--infer-field-validity`, `--infer-array-param-bounds`, `--scale-down`, `--safety-only`), BMC-Agent verifies **22 of 30 functions clean** at scaled-down sizes (`B=T=C=NH=V=Vp=OC=4`), up from 4/30 — including `softmax_forward`, `layernorm_forward/backward`, and `matmul_forward/backward`. To our knowledge this is the first application of bounded model checking to a real ML training program. Full scorecard in [`findings/llm_c/`](findings/llm_c/).

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
  pipeline.py · config.py · spec.py · llm.py     orchestrator, config, spec model, LLM client
  spec_generator.py · spec_quality.py            Phase 1 spec gen; Phase 4 quality checks
  bmc_engine.py · cbmc.py · kani.py · backends/  Phase 2 solver runners (CBMC / Kani)
  cex_validator.py · dynamic_validator.py        Phase 3 classification, feasibility, runtime confirm
  realism_checker.py · bug_reporter.py           Phase 3 realism audit; confidence-tier reporting
  harness_generator.py · dsl_to_cbmc.py          harness synthesis; spec-DSL → CBMC translation
  parser.py · rust_parser.py · source_parser.py  tree-sitter C/Rust parsers + dispatch
  feedback_loop.py · flag_selector.py · domain_analyzer.py · preprocessor.py   optional stages
  evaluation/                                    baselines, metrics, corpus, reports
examples/   synthetic + real-world C / Rust targets
tests/      unit and integration tests
```

## Status

BMC-Agent is an active research prototype; the pipeline and all confidence tiers are stable.

**Working:** full C (CBMC) and Rust (Kani) pipelines with backend dispatch by extension; whole-codebase `verify-dir` with cross-file call-graph construction; the Phase 3 classification → feasibility → dynamic-validation → realism stack with five-tier confidence reporting; CEGAR refinement with callee-stub postconditions and the FilteringOnly ablation (`--skip-refinement`); wire-format C support (`--real-libc`/`--strict-dsl`/`--raw-bytes`, validated on OpenSSL ASN.1, libxml2, jq, protobuf upb); a broad Rust harness generator (slices, `Vec`/`Option`/`&str`, generics, `unsafe fn`, cargo workspaces); the self-improvement feedback loop (`--enable-feedback-loop`); and specification synthesis (`--specs-bench`) of function contracts and loop invariants via CBMC or Frama-C/WP.

**Partial / planned:** spec-quality analysis at scale; evaluation corpus beyond VibeOS / llm.c; manual precision audit of sampled findings; constructor-pattern precondition inference for ML-kernel targets.

## Citation

If you use AProver or BMC-Agent in academic work, please cite the paper that introduces *agentic model checking*:

> Youcheng Sun, Jiawen Liu, Daniel Kroening, and Jason Xue. **Agentic Model Checking.** arXiv:2605.21434, 2026. <https://arxiv.org/abs/2605.21434>

```bibtex
@article{sun2026agentic,
  title   = {Agentic Model Checking},
  author  = {Sun, Youcheng and Liu, Jiawen and Kroening, Daniel and Xue, Jason},
  journal = {arXiv preprint arXiv:2605.21434},
  year    = {2026},
  url     = {https://arxiv.org/abs/2605.21434}
}
```

## License

4-clause BSD. See [LICENSE](LICENSE).
