# AMC — Agentic Model Checking

AMC is a compositional bounded model checker for C programs. It combines [CBMC](https://github.com/diffblue/cbmc) as a conventional BMC solver with an LLM agent that generates function specifications, classifies counterexamples, and refines preconditions.

Each function is verified in isolation: callees are replaced with stubs constrained by their LLM-generated specifications. CBMC then checks the function against its spec and the stubs. This makes verification tractable on real codebases without requiring manual annotations.

## How it works

```
Phase 1  Spec Generator    [AGENTIC]     LLM generates pre/postconditions per function
Phase 2  BMC Engine        [CONVENTIONAL] CBMC checks each function against its spec
Phase 3  CEx Confirmation  [AGENTIC]     LLM classifies: REAL_BUG / SPURIOUS / UNRESOLVED
         Spec Refiner      [AGENTIC]     Refines preconditions on spurious counterexamples
Phase 3b Propagation       [CONVENTIONAL] Re-verifies callers after spec refinement
Phase 3c CEGAR loop        [CONVENTIONAL] Re-verifies refined functions (may unmask real bugs)
```

The agentic components handle semantic reasoning; the conventional BMC engine provides formal guarantees within the unwind bound.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- [CBMC](https://github.com/diffblue/cbmc) (`cbmc` on PATH)
- Anthropic API key

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

# Run evaluation corpus
uv run amc eval --corpus path/to/corpus.json --output artifacts/
```

Artifacts (specs, harnesses, CBMC results, bug reports) are written under `--output`.

## Configuration

All settings can be overridden via environment variables:

| Variable | Default | Description |
|---|---|---|
| `AMC_LLM_MODEL` | `claude-sonnet-4-6` | LLM model |
| `AMC_CBMC_PATH` | `cbmc` | Path to CBMC binary |
| `AMC_CBMC_UNWIND` | `4` | Loop unwind bound |
| `AMC_CBMC_TIMEOUT` | `120` | CBMC timeout (seconds) |
| `AMC_MAX_REFINEMENT_ITERS` | `5` | Max CEGAR iterations |
| `AMC_ENABLE_DUAL_SPEC` | `true` | Generate each spec twice, flag disagreements |
| `AMC_SKIP_REFINEMENT` | `false` | Filtering-only mode (classify but don't refine) |

## Examples

The `examples/` directory contains:

| File | Description |
|---|---|
| `simple_driver.c` | Ring buffer device — off-by-one in `rb_write` |
| `sensor_hub.c` | CEGAR demo — spurious CEx triggers refinement, reveals real bug |
| `block_device.c` | Integer overflow in `blk_seek` |
| `memory_allocator.c` | Null dereference in `alloc_free` |
| `vibeos/vibeos_memory.c` | VibeOS kernel allocator — `calloc` overflow + `malloc` wraparound |
| `vibeos/vibeos_string.c` | VibeOS string functions — 16 unbounded loop bugs |
| `vibeos/vibeos_printf.c` | VibeOS printf — `print_signed` INT64_MIN UB |
| `vibeos/vibeos_vfs.c` | VibeOS VFS — 18 unbounded path traversal bugs |
| `vibeos/vibeos_dtb.c` | VibeOS DTB parser — 4 bugs on untrusted input |

## Evaluation on VibeOS

AMC was run against [VibeOS](https://github.com/kaansenol5/VibeOS), a bare-metal ARM64 OS (~8k LoC, 95% C). **41 confirmed bugs** were found across 5 modules, none previously reported in the issue tracker.

| Module | Bugs |
|---|---|
| `vfs.c` | 18 |
| `string.c` | 16 |
| `dtb.c` | 4 |
| `printf.c` | 3 |
| `klog.c` | 0 |

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
  bmc_engine.py         Phase 2: CBMC runner
  cex_validator.py      Phase 3: counterexample classification
  spec_refiner logic    Phase 3: precondition refinement (in pipeline.py)
  harness_generator.py  CBMC harness synthesis
  dsl_to_cbmc.py        Spec DSL → __CPROVER_assume/assert
  evaluation/           Baselines, metrics, corpus, report generation
  backends/             BMCBackend ABC; CBMCBackend; KaniBackend (stub)
examples/               Synthetic and real-world C targets
tests/                  Unit and integration tests
```

## License

MIT
