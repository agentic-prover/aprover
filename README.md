# AMC — Agentic Model Checking

AMC is a prototype implementation of agentic model checking: an architecture that combines an LLM agent for specification generation, counterexample classification, and spec refinement with a sound bounded model checking backend. The agent handles tasks where natural-language reasoning is appropriate (generating specifications from code, classifying counterexamples, proposing refinements); the BMC backend handles verification itself, preserving formal guarantees within the unwinding bound.

The architecture is backend-agnostic by design: AMC defines a `BMCBackend` abstraction that any BMC tool can implement. The agentic layer — spec generation, counterexample classification, refinement — is independent of which solver is underneath.

Each function is verified in isolation: callees are replaced with stubs constrained by their LLM-generated specifications. The BMC backend then checks the function against its spec and those stubs. This makes verification tractable on real codebases without manual annotations.

## What AMC is, and what it isn't

AMC **is** a research prototype that:
- Runs end-to-end on real-world C, including bare-metal OS kernels.
- Gives you sound per-function verification *within the BMC backend's unwinding bound*, conditional on the LLM-generated specs being correct.
- Produces concrete reproducible counterexamples, not natural-language bug descriptions.
- Supports a filtering-only ablation mode, so you can compare "classify only" against "classify + refine."

AMC **is not** (yet):
- Production-ready. Expect rough edges, failed harnesses on trivial functions, and spec-quality issues that the LLM sometimes introduces.
- A replacement for full formal verification. Soundness is bounded by the unwinding depth and by spec correctness, neither of which AMC proves.
- Evaluated against baselines yet. Baseline comparisons are implemented but not yet reported.

## How it works

```
Phase 1   Spec Generator      [AGENTIC]      LLM generates pre/postconditions per function
Phase 2   BMC Engine          [CONVENTIONAL] Checks each function against its spec
Phase 3   CEx Confirmation    [AGENTIC]      LLM classifies: REAL_BUG / SPURIOUS / UNRESOLVED
          Spec Refiner        [AGENTIC]      Refines preconditions on spurious counterexamples
Phase 3b  Propagation         [CONVENTIONAL] Re-verifies callers after spec refinement
Phase 3c  CEGAR loop          [CONVENTIONAL] Re-verifies refined functions (may unmask bugs)
Phase 4   Spec Quality        [AGENTIC]      Coverage / mutation / consistency checks (optional)
```

The agentic components handle semantic reasoning; the conventional BMC engine provides the formal guarantee. The design principle is *agents propose, conventional tools dispose* — every soundness-relevant decision the LLM proposes passes through a conventional check before affecting the verification verdict.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- A BMC solver on `PATH` (the C backend uses the solver specified by `AMC_BMC_PATH`)
- An Anthropic API key

## Installation

```bash
git clone https://github.com/theyoucheng/amc
cd amc
uv sync
```

## Usage

```bash
export ANTHROPIC_API_KEY=your_key_here

# Verify a C source file
uv run amc verify --source examples/simple_driver.c \
                  --driver simple_driver \
                  --output artifacts/

# Generate specs only (no BMC)
uv run amc generate --source examples/simple_driver.c --driver simple_driver

# Run a corpus
uv run amc eval --corpus path/to/corpus.json --output artifacts/
```

Artifacts — generated specs, BMC harnesses, raw solver output, counterexample classifications, and bug reports — are written under `--output` and are intended to be inspected or diffed.

## Configuration

All settings can be overridden via environment variables or as `Config` dataclass fields.

| Variable | Default | Purpose |
|---|---|---|
| `AMC_LLM_MODEL` | `claude-sonnet-4-6` | LLM model |
| `AMC_CBMC_PATH` | `cbmc` | Path to BMC solver binary |
| `AMC_CBMC_UNWIND` | `4` | Loop unwinding bound |
| `AMC_CBMC_TIMEOUT` | `120` | Solver timeout per function (seconds) |
| `AMC_MAX_REFINEMENT_ITERS` | `5` | Maximum CEGAR iterations |
| `AMC_ENABLE_DUAL_SPEC` | `true` | Generate each spec twice, flag disagreements |
| `AMC_ENABLE_SPEC_QUALITY` | `false` | Run Phase 4 spec-quality checks |
| `AMC_SKIP_REFINEMENT` | `false` | Filtering-only mode (classify but don't refine) |

The `AMC_SKIP_REFINEMENT` toggle is the control for the project's own ablation study: running AMC with and without refinement on the same input measures whether the refinement machinery contributes value beyond simple filtering of spurious counterexamples.

## Examples

The `examples/` directory contains synthetic targets used to validate the pipeline plus VibeOS modules used for the real-world evaluation.

| File | Description |
|---|---|
| `simple_driver.c` | Ring-buffer device — off-by-one in `rb_write` |
| `sensor_hub.c` | CEGAR demo: spurious counterexample triggers refinement, reveals real bug |
| `block_device.c` | Integer overflow in `blk_seek` |
| `memory_allocator.c` | Null dereference in `alloc_free` |
| `vibeos/vibeos_memory.c` | VibeOS kernel allocator — `calloc` overflow and `malloc` wraparound |
| `vibeos/vibeos_string.c` | VibeOS string functions — 16 unbounded-loop findings |
| `vibeos/vibeos_printf.c` | VibeOS printf — `print_signed` `INT64_MIN` UB |
| `vibeos/vibeos_vfs.c` | VibeOS VFS — 18 unbounded-loop findings on path traversal |
| `vibeos/vibeos_dtb.c` | VibeOS DTB parser — 4 findings on untrusted input |

## Preliminary evaluation: VibeOS

AMC was run against [VibeOS](https://github.com/kaansenol5/VibeOS), a bare-metal ARM64 hobby OS written with LLM assistance (about 8k LoC, 95% C). Five kernel modules were verified under `examples/vibeos/`.

**41 findings total** across those modules, none of which appear in the VibeOS public issue tracker as of the evaluation date.

| Module | Findings |
|---|---|
| `vfs.c` | 18 |
| `string.c` | 16 |
| `dtb.c` | 4 |
| `printf.c` | 3 |
| `klog.c` | 0 |

**How to read these numbers.** The 41 count is the tool's classifier output, not an externally audited ground truth. Of the 41:

- 40 are unbounded-loop findings in string- and path-processing functions. Concrete witnesses confirm they are reproducible — e.g., passing a non-null-terminated buffer causes the function to read past the end indefinitely. Whether each of these 40 is a latent vulnerability or a caller-side precondition violation depends on the caller's trust context. For parser code receiving untrusted input (the DTB case), the latent-vulnerability reading clearly applies.
- 1 is an unambiguous memory-safety bug: `print_signed` computes `num = -num` on `INT64_MIN`, which is signed integer overflow (undefined behavior in C). Triggered directly by `printf("%ld", (long)INT64_MIN)`.
- AMC also independently reproduced a `calloc` integer-overflow issue in the memory allocator that is filed in the VibeOS issue tracker. This cross-validation is a small but useful positive signal.

A manual audit of a sampled subset of the 41 findings, plus baseline comparisons, is planned next.

## Running tests

```bash
uv run pytest tests/ -q
# 111 passed, 1 skipped
```

## Project structure

```
amc/                    Core package
  config.py             Configuration dataclass
  pipeline.py           End-to-end orchestrator (AMCPipeline, PropagationEvent)
  spec_generator.py     Phase 1: LLM spec generation
  bmc_engine.py         Phase 2: BMC runner
  cex_validator.py      Phase 3: counterexample classification and refinement
  harness_generator.py  BMC harness synthesis
  dsl_to_cbmc.py        Spec DSL → solver assume / assert calls
  evaluation/           Baselines, metrics, corpus, report generation
  backends/             BMCBackend ABC and concrete backend implementations
examples/               Synthetic and real-world C targets
tests/                  Unit and integration tests
```

## Status

AMC is an active research prototype. The architecture and pipeline are stable; the evaluation and spec-quality components are under active development.

- **Working:** C verification, all four agentic components, filtering-only ablation, parallel solver execution, propagation event tracking.
- **Planned:** Mutation testing and other Phase 4 spec-quality defenses; full evaluation corpus beyond VibeOS; baseline comparisons.

## License

TBD. Intended to be Apache 2.0 or MIT at release.
