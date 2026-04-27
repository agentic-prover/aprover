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
Phase 1    Spec Generator      [AGENTIC]      LLM generates pre/postconditions per function
Phase 2    BMC Engine          [CONVENTIONAL] Checks each function against its spec
Phase 3    CEx Confirmation    [AGENTIC]      LLM classifies: REAL_BUG / SPURIOUS / UNRESOLVED
           Spec Refiner        [AGENTIC]      Refines preconditions on spurious counterexamples
Phase 3 S2 Feasibility check  [CONVENTIONAL] Re-verifies violation with real callee bodies inlined
Phase 3 S3 Dynamic validation [CONVENTIONAL] Compiles + runs GCC harness to confirm fault at runtime
Phase 3b   Propagation        [CONVENTIONAL] Re-verifies callers after spec refinement
Phase 3c   CEGAR loop         [CONVENTIONAL] Re-verifies refined functions (may unmask bugs)
Phase 4    Spec Quality        [AGENTIC]      Coverage / mutation / consistency checks (optional)
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

# CBMC-alone baseline (no LLM, no spec generation — for comparison)
uv run amc baseline --source examples/simple_driver.c \
                    --driver simple_driver \
                    --output artifacts/baseline_simple_driver

# Verify a two-file cross-file demo (confirmed_system_entry across files)
uv run amc verify-dir \
  --source-dir examples/cross_file_demo \
  --driver cross_file_demo \
  --output artifacts/cross_file_demo
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
| `AMC_ENABLE_DYNAMIC_VALIDATION` | `false` | Phase 3 S3: compile + run a GCC harness to confirm real faults |
| `AMC_DYNAMIC_VALIDATION_TIMEOUT` | `30` | Seconds the compiled harness is allowed to run |
| `AMC_DYNAMIC_CC_PATH` | `gcc` | C compiler for dynamic harness compilation |

The `AMC_SKIP_REFINEMENT` toggle is the control for the project's own ablation study: running AMC with and without refinement on the same input measures whether the refinement machinery contributes value beyond simple filtering of spurious counterexamples.

## Examples

The `examples/` directory contains synthetic targets used to validate the pipeline plus VibeOS modules used for the real-world evaluation.

| File | Description |
|---|---|
| `simple_driver.c` | Ring-buffer device — off-by-one in `rb_write` |
| `sensor_hub.c` | CEGAR demo: spurious counterexample triggers refinement, reveals real bug |
| `block_device.c` | Integer overflow in `blk_seek` |
| `memory_allocator.c` | Null dereference in `alloc_free` |
| `cross_file_demo/` | Two-file demo of cross-file `confirmed_system_entry`: `apply_op` (null fn-ptr in `libmath.c`) traced to `system_entry` (in `main.c`) |
| `vibeos/vibeos_memory.c` | VibeOS kernel allocator — `calloc` overflow and `malloc` wraparound |
| `vibeos/vibeos_string.c` | VibeOS string functions — 16 unbounded-loop findings |
| `vibeos/vibeos_printf.c` | VibeOS printf — `print_signed` `INT64_MIN` UB |
| `vibeos/vibeos_vfs.c` | VibeOS VFS — 18 unbounded-loop findings on path traversal |
| `vibeos/vibeos_dtb.c` | VibeOS DTB parser — 4 findings on untrusted input |

## Preliminary evaluation: VibeOS

AMC was run against [VibeOS](https://github.com/kaansenol5/VibeOS), a bare-metal ARM64 hobby OS written with LLM assistance (about 8k LoC, 95% C). Two evaluation modes were exercised.

### Wrapper mode — 70 findings across 7 modules

Hand-crafted per-module wrappers inline cross-file includes and stub external dependencies. Seven kernel modules were verified under `examples/vibeos/`.

| Module | Findings | Notes |
|---|---|---|
| `vibeos_vfs.c` | 18 | Unbounded loops in path and string traversal |
| `vibeos_string.c` | 16 | Unbounded loops in string functions |
| `vibeos_net.c` | 16 | TCP/UDP/DNS parsing bugs |
| `vibeos_elf.c` | 13 | ELF segment offset arithmetic |
| `vibeos_dtb.c` | 4 | Untrusted DTB input; null dereference |
| `vibeos_printf.c` | 3 | `INT64_MIN` signed overflow; two unbounded loops |
| `vibeos_klog.c` | 0 | Ring-buffer logic verified |

### Raw-source mode — ~198 findings across 43 files (partial run)

In raw-source mode AMC preprocesses the unmodified kernel source directly using `cc -E` and verifies each file without any manual wrapper preparation. A partial run across 43 of 48 kernel `.c` files (stopped before `ttf`, `vfs`, `virtio_blk`, `virtio_net`, `virtio_sound`) reported ~198 confirmed bugs, including HAL-layer findings in the SD card driver and interrupt controller.

```bash
amc verify-dir \
  --source-dir examples/vibeos/repo/kernel \
  --driver vibeos_kernel \
  --output artifacts/vibeos_kernel_full \
  --include-dir examples/vibeos/repo/kernel
```

### Raw-source mode with dynamic validation — 81 real bugs in 28/48 files

A second raw-source run with `AMC_ENABLE_DYNAMIC_VALIDATION=true` exercised the complete dynamic validation pipeline directly on the unmodified VibeOS kernel source. The run covered 28 of 48 kernel `.c` files before being stopped.

**Enabling dynamic validation on bare-metal C** required four harness fixes to allow the GCC harness to compile and run against preprocessed ARM64 kernel sources on an x86 host:
- Strip ARM64 inline ASM blocks (`asm volatile`, `__asm__`) that won't assemble on x86.
- Strip `static inline` libc stubs that the kernel defines internally (e.g. `signal()`, `memcpy()`) and that conflict with the system headers the harness includes.
- Strip glibc-internal typedefs (`__fsid_t`, `__dev_t`, `max_align_t`, etc.) from the preprocessed type declarations — the VibeOS in-tree libc headers redefine these identically to glibc, causing duplicate-type errors when the harness also includes `<signal.h>`.
- Strip forward declarations of standard functions with non-standard signatures (e.g. `printf.h` declares `snprintf(char*, int, ...)` instead of `snprintf(char*, size_t, ...)`).
- When kernel functions reference globals from other translation units (e.g. `fb_base` defined in `fb.c`), a third compile attempt uses `-Wl,--unresolved-symbols=ignore-all` so the harness still links and runs.

**Results across 28 files:**

| Confidence tier | Count | Description |
|---|---|---|
| `confirmed_dynamic` | 6 | Runtime fault (SIGSEGV) directly observed |
| `confirmed_system_entry` | 56 | Full call chain traced to kernel entry point |
| `confirmed_bmc` | 19 | BMC reachability confirmed from a caller |
| **Total** | **81** | |

**`confirmed_dynamic` bugs (runtime SIGSEGV):** `console.scroll_up`, `cursor.draw_cursor_at`, `cursor.save_background`, `elf.elf_load`, `elf.elf_load_at`, `gpio.gpio_delay_us`.

To reproduce:
```bash
AMC_ENABLE_DYNAMIC_VALIDATION=true amc verify-dir \
  --source-dir examples/vibeos/repo/kernel \
  --driver vibeos_dynamic \
  --output artifacts/vibeos_dynamic \
  --include-dir examples/vibeos/repo/kernel
```

**FilteringOnly baseline (RQ3).** On the five wrapper modules, the `--skip-refinement` ablation (classify only, no spec update, no caller re-queue) reported 59 findings vs. AMC's 41 — a 44% increase. Refinement acts as a precision filter on this corpus, not a recall enhancer.

**How to read these numbers.** All finding counts are the tool's own classification — not an externally audited ground truth. Each finding comes with a concrete counterexample (specific input values) produced by the BMC solver: the witness is a real execution path, not a probabilistic estimate. Phase 3 applies a three-stage soundness check: (1) reachability harness stubs are constrained by callee postconditions via `__CPROVER_assume`, (2) a Stage 2 feasibility check re-verifies the violation with real local callee bodies inlined, and (3) an optional Stage 3 GCC harness directly confirms the fault at runtime. Each confirmed real bug is assigned one of four evidence tiers: `confirmed_dynamic` (runtime fault observed), `confirmed_system_entry` (full call chain traced back to a function with no callers in any file), `confirmed_bmc` (reachability confirmed from at least one caller via BMC), or `likely` (over-refinement guard triggered). In whole-codebase mode (`verify-dir`), AMC performs a two-pass global call-graph construction so that functions whose callers reside in other files are not misclassified as system entry points. The `confirmed_system_entry` tier is demonstrated end-to-end in `examples/cross_file_demo/`: `apply_op` (null function-pointer dereference in `libmath.c`) is confirmed reachable from `system_entry` in `main.c` via cross-file CBMC reachability, yielding `confirmed_system_entry` with chain `system_entry → apply_op`. A manual precision audit of a sampled subset is the immediate next step.

AMC independently reproduced the `calloc` integer-overflow issue filed in the VibeOS tracker (issue #26), cross-validating at least one finding against an independent source.

### Limitations

- **Callee stub soundness (partial).** Phase 2 and Phase 3 reachability stubs are now constrained by callee postconditions, and Phase 3 Stage 2 inlines real local callee bodies for feasibility checking. External callees (hardware registers, OS syscalls) still use postcondition-constrained stubs; their postconditions are LLM-generated and may be over-permissive.
- **No baseline comparisons reported yet.** The three ablation baselines are implemented but not yet exercised on VibeOS.
- **Raw-source run (without dynamic validation):** ~198 findings across 43/48 files. Dynamic-validation run: 81 findings across 28/48 files (stopped mid-run).
- **Single-system evaluation.** Generalization beyond VibeOS is not yet demonstrated.

## Running tests

```bash
uv run pytest tests/ -q
# 198 passed, 1 skipped
```

## Usage — whole-codebase mode

To verify an entire C source directory without manual wrapper preparation:

```bash
# Preprocess and verify every .c file in the kernel directory
uv run amc verify-dir \
  --source-dir path/to/kernel \
  --driver my_project \
  --output artifacts/ \
  --include-dir path/to/kernel
```

AMC runs in two passes over the source directory. Pass 1 preprocesses and parses every `.c` file to build a global call graph, identifying which functions have callers in other files. Pass 2 runs the full verification pipeline per file, using the global graph to prevent HAL functions and other cross-file-called helpers from being misclassified as system entry points. The `-I` paths are forwarded to both the C preprocessor and the BMC solver so headers resolve correctly.

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

- **Working:** C verification, all four agentic components, filtering-only ablation (`--skip-refinement`), parallel solver execution, propagation event tracking, whole-codebase raw-source mode (`verify-dir`), two-pass global call-graph construction for cross-file caller awareness, cross-file CBMC reachability queries (chain continues through caller files to a true system entry, promoting findings to `confirmed_system_entry`), callee stub postcondition constraints (`__CPROVER_assume`), Phase 3 Stage 2 feasibility check (real callee bodies inlined), Phase 3 Stage 3 dynamic validation (GCC harness confirms fault at runtime), four-tier confidence reporting (`confirmed_dynamic` > `confirmed_system_entry` > `confirmed_bmc` > `likely`), prompt caching.
- **Working:** spec-quality module (`AMC_ENABLE_SPEC_QUALITY=true`) with mutation testing, coverage checks, consistency checks, and executable sanity checks; `amc baseline` command for CBMC-alone comparison runs.
- **Partial:** External callee postconditions are LLM-generated and may be over-permissive; spec-quality analysis has not yet been run at scale.
- **Planned:** Full evaluation corpus beyond VibeOS; CBMCAloneBaseline and AMCAblationBaseline comparisons on VibeOS.

## License

TBD. Intended to be Apache 2.0 or MIT at release.
