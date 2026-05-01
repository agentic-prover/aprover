<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <img alt="AProver" src="assets/logo.svg" height="80">
</picture>

**AProver** is a suite of LLM-driven formal verification agents. The first agent — **BMC-Agent** — is a prototype of *agentic model checking*: an architecture that pairs an LLM agent (for specification generation, counterexample classification, and spec refinement) with a sound bounded model checking backend. The agent handles semantic reasoning; the solver provides formal guarantees within the unwinding bound.

The design principle is *agents propose, conventional tools dispose*: every soundness-relevant decision the LLM produces passes through a conventional check (CBMC query, SMT soundness guard, or runtime confirmation) before affecting the verification verdict.

## How it works

```
Phase 1    Spec Generator        [AGENTIC]       LLM generates pre/postconditions top-down per function
Phase 2    BMC Engine            [CONVENTIONAL]  Checks each function against its spec via CBMC
Phase 3 S1 CEx Classifier        [AGENTIC]       LLM + CBMC: REAL_BUG / SPURIOUS / UNRESOLVED
Phase 3 S2 Feasibility check     [CONVENTIONAL]  Re-verifies violation with real callee bodies inlined
Phase 3 S3 Dynamic validation    [CONVENTIONAL]  Compiles + runs GCC harness to confirm fault at runtime
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

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- [CBMC](https://github.com/diffblue/cbmc) on `PATH`
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

# Generate specs only
uv run bmc-agent generate --source examples/simple_driver.c --driver simple_driver

# Verify every .c file in a directory (whole-codebase mode)
uv run bmc-agent verify-dir \
  --source-dir examples/vibeos/repo/kernel \
  --driver vibeos_kernel \
  --output artifacts/vibeos_kernel \
  --include-dir examples/vibeos/repo/kernel

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

## Examples

| File | Description |
|---|---|
| `simple_driver.c` | Ring-buffer device — off-by-one in `rb_write` |
| `sensor_hub.c` | CEGAR demo: spurious counterexample → refinement → real bug |
| `block_device.c` | Integer overflow in `blk_seek` |
| `memory_allocator.c` | Null dereference in `alloc_free` |
| `cross_file_demo/` | Cross-file `confirmed_system_entry`: null fn-pointer in `libmath.c` traced to `system_entry` in `main.c` |
| `vibeos/vibeos_memory.c` | VibeOS allocator — `calloc` overflow (cross-validates tracker issue #26) |
| `vibeos/vibeos_string.c` | 16 unbounded-loop findings in string functions |
| `vibeos/vibeos_printf.c` | `print_signed` `INT64_MIN` signed overflow |
| `vibeos/vibeos_vfs.c` | 18 unbounded-loop findings in path traversal |
| `vibeos/vibeos_dtb.c` | 4 findings on untrusted DTB input |

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
  bmc_engine.py         Phase 2: CBMC runner with parallel dispatch
  cex_validator.py      Phase 3: counterexample classification + refinement loop
  harness_generator.py  CBMC and GCC harness synthesis
  dsl_to_cbmc.py        Spec DSL → __CPROVER_assume / assert translation
  dynamic_validator.py  Phase 3 S3: GCC harness compile + run
  spec_quality.py       Phase 4: mutation testing, coverage, consistency checks
  evaluation/           Baselines, metrics, corpus, report generation
  backends/             BMCBackend ABC + CBMCBackend (KaniBackend scaffolded)
examples/               Synthetic and real-world C targets
tests/                  220 unit and integration tests
```

## Status

BMC-Agent is an active research prototype. The pipeline and all four evidence tiers are stable; the evaluation and spec-quality components are under active development.

**Working:** full C verification pipeline, whole-codebase `verify-dir` mode, cross-file call-graph construction, cross-file CBMC reachability, Phase 3 Stage 2 feasibility check (real callee inlining), Phase 3 Stage 3 dynamic validation (bare-metal-compatible GCC harness), four-tier confidence reporting, callee stub postcondition constraints, FilteringOnly ablation (`--skip-refinement`), spec-quality module (`BMC_AGENT_ENABLE_SPEC_QUALITY=true`), prompt caching.

**Partial / planned:** spec-quality analysis at scale; CBMCAloneBaseline and AMCAblationBaseline comparisons on VibeOS; Kani (Rust) backend; evaluation corpus beyond VibeOS; manual precision audit of sampled findings.

## License

TBD. Intended to be Apache 2.0 or MIT at release.
