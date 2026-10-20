<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <img alt="AProver" src="assets/logo.svg" height="80">
</picture>

**Agentic formal verification for AI-generated and human-written code.**

[Try AProver](https://www.aprover.ai) · [Paper](https://arxiv.org/abs/2605.21434) · [Quick start](#quick-start) · [Accepted Linux fixes](#linux-fixes-accepted-upstream)

AProver combines LLM agents with formal verification tools. Its first agent,
**BMC-Agent**, generates function contracts, checks code with bounded model
checking, and uses counterexamples to refine specifications and investigate
potential bugs. It supports **C with CBMC**, **Rust with Kani**, and **Java with
JBMC**. The agentic function-contract pipeline applies to C and Rust; Java
currently uses whole-program verification.

## Linux fixes accepted upstream

**13 BMC-Agent-assisted Linux driver fixes have been accepted upstream:
8 in mainline and 5 in linux-next**, as verified on **October 20, 2026**.
The findings were reviewed and the fixes submitted by Jiawen Liu, with
revisions following maintainer feedback.

Selected fixes:

| Driver | Fix | Status | Commit |
| --- | --- | --- | --- |
| Hisilicon hix5hd2 GMAC | Remove premature, redundant NAPI deletion during teardown | Mainline | [f307a7dc3209](https://github.com/torvalds/linux/commit/f307a7dc32097c11413178fca437a10d20890bc2) |
| NXP/Freescale DSPI | Unwind failed suspend and resume operations | Mainline | [2543355f3da5](https://github.com/torvalds/linux/commit/2543355f3da56a297e3803ccb5d29f4dce5f18f1) |
| StarFive crypto | Handle clock-enable failures during probe | linux-next | [5358248961ae](https://github.com/external-mirrors/linux-next/commit/5358248961ae23f7253d010ddfde1cfd2556ce22) |
| Exynos FIMD | Release the bus clock when resume fails | linux-next | [d04392ce52d3](https://github.com/external-mirrors/linux-next/commit/d04392ce52d33bce4d532bd8b965151ed4ce8670) |
| Amphion VPU | Remove an extra runtime-PM put on probe failure | linux-next | [0cabf27cf1e6](https://github.com/external-mirrors/linux-next/commit/0cabf27cf1e6f7ee5f3c5893c0f1930814d949af) |

<details>
<summary><strong>View all 13 accepted fixes</strong></summary>

| Subsystem | Driver | Fix | Status | Commit |
| --- | --- | --- | --- | --- |
| SPI | [bcm63xx](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/spi-bcm63xx-suspend_finding.md) | Propagate controller-suspend failure before clock teardown | Mainline | [bbbc1ef5b353](https://github.com/torvalds/linux/commit/bbbc1ef5b35341c5122418a14f8924688a4bb954) |
| SPI | [bcm63xx-hsspi](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/spi-bcm63xx-hsspi-suspend_finding.md) | Preserve PLL and controller clocks when suspension fails | Mainline | [3b5414d76a03](https://github.com/torvalds/linux/commit/3b5414d76a0381db77b9694537254af7acfa187a) |
| SPI | [bcm-qspi](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/spi-bcm-qspi-suspend_finding.md) | Stop hardware teardown when controller suspension fails | Mainline | [44d09ef0ba69](https://github.com/torvalds/linux/commit/44d09ef0ba690ac46d17eb05c12a47a5dd75e231) |
| SPI | [atcspi200](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/spi-atcspi200-suspend_finding.md) | Return controller-suspend errors before disabling the clock | Mainline | [f4b14e67baff](https://github.com/torvalds/linux/commit/f4b14e67baffd2c879765275bda998a183594e8b) |
| SPI | [bcmbca-hsspi](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/spi-bcmbca-hsspi-suspend_finding.md) | Preserve clocks when controller suspension fails | Mainline | [faa878d4805c](https://github.com/torvalds/linux/commit/faa878d4805ca375738294bc52eae310eb287b16) |
| SPI | [fsl-dspi](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/spi-fsl-dspi-pm-unwind_finding.md) | Unwind failed suspend and resume operations | Mainline | [2543355f3da5](https://github.com/torvalds/linux/commit/2543355f3da56a297e3803ccb5d29f4dce5f18f1) |
| SPI | [nxp-fspi](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/spi-nxp-fspi-probe-pm_finding.md) | Disable runtime PM and balance probe-failure cleanup | Mainline | [5d14285d60ba](https://github.com/torvalds/linux/commit/5d14285d60ba95561609a293064654a0c7d7d928) |
| Networking | [hix5hd2_gmac](https://github.com/agentic-prover/aprover/blob/main/findings/linux_drivers/hix5hd2-gmac-napi-teardown_finding.md) | Remove premature, redundant NAPI deletion during teardown | Mainline | [f307a7dc3209](https://github.com/torvalds/linux/commit/f307a7dc32097c11413178fca437a10d20890bc2) |
| Crypto | StarFive jh7110-cryp | Handle clock-enable failures and unwind partial acquisition | linux-next | [5358248961ae](https://github.com/external-mirrors/linux-next/commit/5358248961ae23f7253d010ddfde1cfd2556ce22) |
| DRM | Exynos FIMD | Release the bus clock when LCD-clock enable fails during resume | linux-next | [d04392ce52d3](https://github.com/external-mirrors/linux-next/commit/d04392ce52d33bce4d532bd8b965151ed4ce8670) |
| Memory | Exynos5422 DMC | Check clock-enable failures and release the first clock if the second fails | linux-next | [cce46902ea9f](https://github.com/external-mirrors/linux-next/commit/cce46902ea9fcd585cd200fffa3166f63bc571f1) |
| SPI | Rockchip SFC | Add runtime-PM teardown to the remove callback | linux-next | [34e32e9ade58](https://github.com/external-mirrors/linux-next/commit/34e32e9ade58e32daf933974c9b697d066fc5aaa) |
| Media | Amphion VPU | Remove an extra runtime-PM put after failed resume-and-get in probe | linux-next | [0cabf27cf1e6](https://github.com/external-mirrors/linux-next/commit/0cabf27cf1e6f7ee5f3c5893c0f1930814d949af) |

### Status definitions

- **Mainline:** the fix is in Linus Torvalds’s Linux history. This does not by itself identify the first released kernel containing it.
- **linux-next:** the fix has been accepted into a subsystem tree and appears in linux-next; it is tracked separately from mainline. Commit links use the public linux-next mirror.

The table records upstream acceptance. Individual findings distinguish solver evidence, source review, and runtime reproduction.

</details>

## Other case studies

| Target | Scope | Reported result |
| --- | --- | --- |
| [VibeOS](https://github.com/agentic-prover/aprover/tree/main/findings/vibeos) | 37 kernel modules, 675 functions | 13 documented bug findings, including 5 crashes confirmed by runtime harnesses |
| [llm.c](https://github.com/agentic-prover/aprover/tree/main/findings/llm_c) | 30 functions in `train_gpt2.c` | 22/30 functions verified for memory safety at scaled-down sizes, up from a 4/30 baseline |

The llm.c results use `B = T = C = NH = V = Vp = OC = 4`.
The linked reports provide the per-finding evidence and per-function scorecards.

## Quick start

For C verification, install **Python 3.12+**, [uv](https://github.com/astral-sh/uv),
and [CBMC](https://github.com/diffblue/cbmc), with `cbmc` on your `PATH`.
The example below uses an Anthropic API key.

```bash
git clone https://github.com/agentic-prover/aprover
cd aprover
uv sync

export ANTHROPIC_API_KEY=your_key_here

uv run bmc-agent verify \
  --source examples/simple_driver.c \
  --driver simple_driver \
  --output artifacts/
```

Inspect generated specifications, verification harnesses, solver results, and
bug reports under `artifacts/`. For a browser-based run, use the
[online workbench](https://www.aprover.ai) with your own API key.

See the [usage and configuration guide](#usage-and-configuration) for directory scans,
Rust inputs, other LLM providers, and specification synthesis. Rust requires
Kani; Java requires JBMC and a JDK. Frama-C/WP is optional for deductive
specification synthesis.

## How it works

1. **Generate specifications.** An LLM agent proposes function contracts from source context.
2. **Check with a solver.** CBMC or Kani checks functions against their contracts and produces counterexamples when properties fail.
3. **Investigate and refine.** The pipeline classifies counterexamples, checks feasibility, and refines contracts to reduce spurious findings. Runtime validation and realism review provide additional evidence when enabled.
4. **Keep the evidence.** Reports retain specifications, harnesses, counterexamples, and validation results for inspection.

The principle is **agents propose, conventional tools check**. Verification
results depend on the contracts, environment models, and solver bounds;
reports distinguish solver evidence from runtime confirmation.
AProver is an active research prototype.

## Usage and configuration

<details>
<summary>Expand CLI examples, provider settings, specification synthesis, and configuration</summary>

Commands below are run from the repository root.

### Requirements

Python 3.12+ and uv are required. Install CBMC for C, Kani for Rust, or JBMC and a JDK for Java. Frama-C and an SMT prover are optional for deductive specification synthesis. Configure an LLM API provider or a supported local CLI provider as described below.

### Usage

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
# --provider claude-code / --provider codex to route every role through a local CLI.
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

#### Web chat (no setup)

For a zero-configuration experience, `web/` contains a chat front-end that lets visitors run AProver by talking to it. The page streams pipeline progress (parse → spec → CBMC → classify → report) live as the agent works.

```bash
ANTHROPIC_API_KEY=sk-... uv run uvicorn web.server:app --port 7860
# open http://localhost:7860
```

Deploy as a Hugging Face Space with `web/deploy_to_space.sh` — see `web/README.md` for the full guide.

#### Agentic mode and post-hoc components

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

### Specification synthesis

Instead of hunting bugs, BMC-Agent can synthesize the **specifications** that make a program's assertions provable. Given a file whose goals are written as `//@ assert`, `assert(...)`, `static_assert`, or `__VERIFIER_assert`, the `--specs-bench` preset auto-dispatches by program content and emits ACSL:

- **no loops → function-contract synthesis** — propose a postcondition, check it makes the asserts hold (sufficiency) and is implied by the body (soundness), then strengthen toward the exact input/output relation.
- **loops → loop-invariant synthesis** — propose inductive invariants (scalar, quantified-array, or recursive-fold) sufficient to discharge the goals.

```bash
# Synthesize + verify with the Frama-C/WP deductive oracle
uv run bmc-agent verify --source prog.c --driver prog.c --agentic --specs-bench --oracle frama-c
```

`--specs-bench` turns on `--math-ints` (the mathematical-integer semantics these benchmarks assume). Overflow-rigor is on by default: a math-int result is re-verified with RTE on and reported as either machine-int **sound** or **math-int only** when the body genuinely overflows (`--no-overflow-rigor` to opt out). `--oracle cbmc` (the default off-preset) handles bounded loops; `--oracle frama-c` handles unbounded loops and quantified/overflow contracts.

### Configuration

All settings are available as environment variables or `Config` dataclass fields.

| Variable | Default | Purpose |
|---|---|---|
| `BMC_AGENT_LLM_MODEL` | `claude-sonnet-4-6` | LLM model |
| `BMC_AGENT_LLM_PROVIDER` | _(auto)_ | LLM provider for all roles: `anthropic`, `openai` (K2 / OpenAI-compatible), `claude-code` (local `claude` CLI), or `codex` (local `codex exec`), with no API key for local CLI modes. Empty = auto-detect. Per-role override: `BMC_AGENT_LLM_<ROLE>_PROVIDER` for `SPEC_GEN`/`REFINEMENT`/`REALISM`/… (CLI sugar: `--provider`, `--specs-via-claude-code`) |
| `BMC_AGENT_CLAUDE_CODE_BIN` | `claude` | Path to the Claude Code CLI (used only when provider is `claude-code`) |
| `BMC_AGENT_CLAUDE_CODE_TIMEOUT_S` | `600` | Per-call timeout for the `claude -p` path (seconds) |
| `BMC_AGENT_CLAUDE_CODE_AGENTIC` | `false` | Let the `claude-code` provider use read-only tools (`Read`/`Grep`/`Glob`) to explore the source tree while drafting/refining specs, instead of a one-shot text completion (CLI: `--claude-code-agentic`) |
| `BMC_AGENT_CLAUDE_CODE_TOOLS` | `Read,Grep,Glob` | Tool allowlist for agentic claude-code mode (keep read-only) |
| `BMC_AGENT_CODEX_BIN` | `codex` | Path to the Codex CLI (used only when provider is `codex`) |
| `BMC_AGENT_CODEX_TIMEOUT_S` | `600` | Per-call timeout for the `codex exec` path (seconds) |
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

### Specification DSL

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


For configuration fields beyond the table above, see [`bmc_agent/config.py`](https://github.com/agentic-prover/aprover/blob/main/bmc_agent/config.py). The [Nix development environment](https://github.com/agentic-prover/aprover/blob/main/nix/tools.nix) provisions the verification tools.

</details>

## Documentation

- [Usage and configuration](#usage-and-configuration): CLI examples, providers, specification synthesis, and the specification DSL.
- [Accepted Linux fixes](#linux-fixes-accepted-upstream): the complete table with upstream commit links.
- [Pipeline details](https://github.com/agentic-prover/aprover/blob/main/PIPELINE.md): verification stages and implementation.
- [Web interface](https://github.com/agentic-prover/aprover/blob/main/web/README.md): local hosting and deployment.

For development, run `uv run pytest tests/ -q`.

## Citation

If you use AProver or BMC-Agent in research, please cite
[*Agentic Model Checking*](https://arxiv.org/abs/2605.21434).

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
