# ESBMC backend and saved-harness replay

BMC-Agent supports ESBMC 8.3.0 as a bounded C verification backend. CBMC is the default.

## Selecting the backend

```sh
python -m bmc_agent.cli verify \
  --source example.c --driver example --output artifacts/example-esbmc \
  --c-backend esbmc --esbmc-path /path/to/esbmc
```

Alternatively set `BMC_AGENT_C_BACKEND=esbmc` and `BMC_AGENT_ESBMC_PATH`.
Python callers can use `Config(c_backend="esbmc", esbmc_path="/path/to/esbmc")`.
The historical `cbmc_unwind`, `cbmc_timeout`, include directories, definitions
and per-function check settings apply to either C backend. Existing SVCOMP
unwind/timeout floors and `BMC_UNWIND_CAP` remain effective. Java/JBMC and
Rust/Kani selection is unchanged.

The selected backend is used by the C engine, caller reachability, feasibility,
over-refinement checks, assertion-driven specifications, invariant entailment,
judge/refinement queries, and the agentic harness frontend check. The explicitly
named CBMC-alone evaluation baseline continues to invoke CBMC.
Legacy artifact names such as `cbmc_result.json` are retained for compatibility.

## Supported semantics and explicit limits

- Assertion/assumption intrinsics map from `__CPROVER_assert/assume` to
  `__ESBMC_assert/assume` using compiler definitions. The saved C file is not edited.
- Generated `__CPROVER_allocate(size, 0)` calls use fresh native ESBMC heap
  allocations, with a local nonnull assumption. The contents remain uninitialized.
  When the source defines its own `malloc`, that symbol and all its uses are
  consistently renamed before the bridge uses `__builtin_malloc`; ordinary
  benchmark allocations retain their original behavior. A declaration without
  a definition keeps the native `malloc` name. A nonzero or variable second
  argument is explicitly rejected by a compile-time assertion.
- Ordinary C assertions, benchmark `__VERIFIER_*` helpers, and explicitly defined
  benchmark helper bodies are passed through to the ESBMC frontend.
- ILP32/LP64, entry function, unwind, wall-clock limit, includes/defines, bounds,
  pointer, division-by-zero, signed/unsigned overflow and undefined-shift checks
  are supported. ESBMC's unrelated default checks are explicitly disabled.
- ESBMC normally checks unwinding assertions. The historical bounded bug-finding
  profile maps to `--partial-loops --no-unwinding-assertions`. A clean result in
  that profile is only the absence of a counterexample in that bounded query.
- Counterexamples are normalized to the existing result schema, with source
  location, assignments, trace and property family. Unwinding failures retain
  the `.unwind.<number>` shape used by the pipeline's filters. Numerical IDs are
  adapter IDs, not CBMC property IDs.
- In guarded LDV mode, a source body with exactly one assertion in `reach_error`
  gives that assertion the stable ID `reach_error.assertion.1`. Only assertion
  failures located in that function are normalized; traces and source locations
  remain unchanged. This lets the primary query and temporary feasibility query
  identify the same original target. Unrelated assertions and ambiguous bodies
  cannot supply target confirmation.
- Feasibility rechecks enable the intrinsic safety check corresponding to the
  original ESBMC counterexample. An unrelated property family or unwinding-only
  failure is inconclusive, rather than confirming the original witness.
- Unsupported CBMC intrinsics (including validity,
  havoc-object and quantified primitives), conversion checks, pointer-overflow
  checks and explicit `object_bits` return errors. They are not silently removed
  or rerouted to CBMC. In particular, standalone mode currently requests
  pointer-overflow checking and therefore reports this unsupported option.
- Timeout, frontend errors, unresolved verification intrinsics, malformed/missing
  violation reports, and successful runs with no VCC evidence are inconclusive.
  The last rule is conservative: constant-folded trivial proofs can also become
  inconclusive. Full-pipeline fallback to LLM reasoning follows the existing
  camera-ready policy; a solver error is not a solver proof.

Each invocation gets a separate temporary working directory for ESBMC's
`report.json`. There is no concurrent report overwrite. Backend memory/library
models and incidental checks can differ even when the target assertion, C file,
architecture and unwind are fixed; this adapter does not claim semantic identity
between all CBMC and ESBMC internals.

## Guarded LDV integration

The formal `rq1-ldv-guarded` profile accepts `--c-backend esbmc --esbmc-path
/path/to/esbmc`. It uses the same input lock, agent configuration, inline/stub
decisions, strict evidence grading and per-function effective unwind as CBMC.
The main and feasibility queries both use ESBMC. CBMC-specific output/refinement
options and its crash retry are not sent to ESBMC, and there is no backend fallback.
For self-contained kernel feasibility harnesses that already define `size_t`,
ESBMC omits the generated `stddef.h` include: Clang otherwise rejects the
benchmark's unsigned-long typedef against its unsigned-int typedef on ILP32.
The benchmark type is retained, and CBMC's harness bytes are unchanged.

See the [LDV experiment data](../artifacts/rq1/README.md#linux-driver-verification) for the per-task outcomes and configurations.

## Reusing existing LLM results without API calls

The generated harness already embodies the specifications, stub/inline decisions
and modeled inputs. It can be checked again without regenerating them. Replay
requires a matching SHA-256 in the original `cbmc_invocations.jsonl`; it refuses
to label a changed, overwritten harness as the same input. It uses the latest
matching recorded invocation (and records its index).

```sh
python scripts/replay_esbmc.py \
  --harness /path/to/case/artifacts/agdrv/main/harness.c \
  --invocations /path/to/rq1-aws/cbmc_invocations.jsonl \
  --esbmc-path /path/to/esbmc \
  --compare-cbmc --timeout 120 \
  --output artifacts/esbmc-paired-example
```

Repeat `--harness` to include multiple saved files from the same invocation log.
The output directory must be new. Both backends receive the same explicitly
chosen wall-clock cap; the saved unwind, architecture and requested checks are
preserved. CBMC uses its original recorded command, with only the executable
and relocated source path optionally changed. ESBMC uses its native SMT encoding
instead of CBMC's `--refine/--slice-formula` encoder switches. Use
`--allow-object-encoding-change` to explicitly omit a recorded CBMC `object_bits`
setting for ESBMC; the original value and difference are then saved in the result.
This flag does not remove any other unsupported check.

The replay script does not instantiate an LLM client. It saves the source copy,
original invocation, adapter code hashes, tool versions, effective command,
parsed result, raw logs, witness JSON and wall time. `llm_calls` and
`llm_cost_usd` are zero. For sources that include external files, those files must
also be kept fixed; hashing the main C file alone does not identify its headers.

Cached LLM responses cannot generally replace later validation/refinement calls:
ESBMC may produce a different witness, changing the next prompt. Saved harness
replay tests the backend query layer. It does not by itself rerun or validate the
entire report-classification pipeline.

## Tests

```sh
TEST_ESBMC_PATH=/path/to/esbmc python -m pytest tests/test_esbmc.py -q
```

The tests cover dispatch, frontend selection, flag behavior, normalized traces,
unwind filtering, failure handling, concurrent reports, source identity and real
engine/refinement queries without CBMC or LLM calls. Installed-backend tests skip
when ESBMC is unavailable.
