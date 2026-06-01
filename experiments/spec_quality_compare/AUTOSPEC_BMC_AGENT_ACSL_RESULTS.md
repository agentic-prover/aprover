# AutoSpec Reproduction and BMC-Agent ACSL Comparison

Date: 2026-06-01

## Purpose

This branch adds an optional ACSL/Frama-C evaluation backend for BMC-Agent and
uses AutoSpec as the first public ACSL benchmark baseline.

The decision question is narrow:

> If BMC-Agent emits ACSL instead of its original DSL, can we compare its spec
> quality against ACSL-oriented work such as AutoSpec, and does this disturb the
> original DSL/CBMC pipeline?

Short answer:

- The original DSL/CBMC pipeline is not replaced.
- DSL-to-ACSL projection preserves final BMC decisions on the supported replay
  subset tested so far.
- Native ACSL generation works end to end, but is currently much weaker than
  AutoSpec on AutoSpec's ACSL/Frama-C benchmark, mainly because many generated
  annotations are syntactically rejected by Frama-C or too weak for WP.

## Branch Scope

This is an experiment/evaluation backend, not a production verifier switch.

Included:

- `bmc_agent/acsl.py`: DSL-to-ACSL bridge and Frama-C/WP runner.
- `bmc_agent/acsl_native.py`: native ACSL spec artifact, annotation injection,
  quality metrics, mutation/VDR helpers.
- `bmc_agent/acsl_cli.py`: optional CLI commands:
  - `bmc-agent acsl-pilot`
  - `bmc-agent acsl-generate`
  - `bmc-agent acsl-quality`
- `experiments/spec_quality_compare/`: AutoSpec reproduction and ACSL quality
  comparison runners.
- `experiments/acsl_dsl_outcome_compare/`: DSL-vs-ACSL final-outcome replay.

Explicitly not included:

- SV-COMP witness-generation adapters.
- VibeOS/QEMU validation changes.
- Generated large artifacts, Docker outputs, or API keys.

## Toolchain and Secrets

AutoSpec reproduction uses:

- AutoSpec Zenodo artifact `10912658`.
- Frama-C Docker image: `framac/frama-c:26.0.debian`.
- Frama-C version: `26.0 (Iron)`.
- Why3 version: `1.5.1`.
- Z3 in image: `4.8.10`.
- AutoSpec README requested Z3: `4.8.6`.
- AutoSpec Python env under `/mnt/disk7/jw_bmc/spec_quality_data/autospec_env`.
- OpenRouter-compatible API through a local secret env file.

No API key is stored in the repository. The expected local secret file is:

```bash
/mnt/disk7/jw_bmc/secrets/openrouter.env
```

The experiment scripts read this file at runtime and redact key-like values in
logs.

## AutoSpec Benchmark Shape

The manifest is built from the official AutoSpec artifact zip.

Artifact-derived case counts:

| Case set | Count |
|---|---:|
| `official_251` candidates | 251 |
| `verified_annotations` | 250 |
| `mutants_100` | 110 |
| `mutant_seeds` | 22 |
| `x509_extra` | 6 |

The `official_251` candidates are distributed as:

| Family | Count |
|---|---:|
| `code2inv_133` | 133 |
| `fib_46` | 46 |
| `frama_c_problems` | 51 |
| `svcomp` | 21 |

AutoSpec success means: generated ACSL annotations are inserted into the C
program, Frama-C/WP runs with AutoSpec's WP flags, and all reported WP goals are
proved. It is not a dynamic test or bug-finding verdict.

## How to Run

Preflight:

```bash
uv run python experiments/spec_quality_compare/autospec_full_repro.py preflight \
  --output artifacts/spec_quality_benchmark/autospec_full/preflight.json \
  --smoke-llm
```

Build the manifest:

```bash
uv run python experiments/spec_quality_compare/autospec_full_repro.py manifest \
  --output artifacts/spec_quality_benchmark/autospec_full/manifest.json
```

Reconcile the artifact's bundled raw results:

```bash
uv run python experiments/spec_quality_compare/autospec_full_repro.py reconcile-raw \
  --output artifacts/spec_quality_benchmark/autospec_full/raw_reconciliation.json
```

Validate AutoSpec's official annotated outputs:

```bash
uv run python experiments/spec_quality_compare/autospec_full_repro.py validate-verified \
  --manifest artifacts/spec_quality_benchmark/autospec_full/manifest.json \
  --case-set verified_annotations \
  --output artifacts/spec_quality_benchmark/autospec_full/verified_validation_full \
  --timeout 120 \
  --workers 4 \
  --cpus 2
```

Run AutoSpec on the 10-case pilot:

```bash
uv run python experiments/spec_quality_compare/autospec_full_repro.py run-autospec \
  --manifest artifacts/spec_quality_benchmark/autospec_full/manifest.json \
  --case-set pilot10 \
  --output artifacts/spec_quality_benchmark/autospec_full/autospec_gpt35_pilot10 \
  --model gpt-3.5-turbo \
  --method autospec_gpt35_openrouter \
  --timeout 600 \
  --workers 1
```

Run BMC-Agent native ACSL on the same pilot:

```bash
uv run python experiments/spec_quality_compare/autospec_full_repro.py run-ours \
  --manifest artifacts/spec_quality_benchmark/autospec_full/manifest.json \
  --case-set pilot10 \
  --output artifacts/spec_quality_benchmark/autospec_full/bmc_agent_acsl_pilot10 \
  --model claude-sonnet-4-6 \
  --timeout 600
```

For longer runs, use `screen`, for example:

```bash
screen -dmS bmc_acsl_stratified50 sh -lc '
  cd <aprover repo> &&
  source /mnt/disk7/jw_bmc/secrets/openrouter.env &&
  uv run python experiments/spec_quality_compare/autospec_full_repro.py run-ours \
    --manifest artifacts/spec_quality_benchmark/autospec_full/manifest.json \
    --case-set bmc_agent_stratified50 \
    --output artifacts/spec_quality_benchmark/autospec_full/bmc_agent_acsl_stratified50 \
    --model claude-sonnet-4-6 \
    --timeout 600
'
```

## AutoSpec Reproduction Results

### Raw artifact reconciliation

The official artifact contains `1873` `final_result` files under raw output
folders. Reconciliation parses these files without rerunning LLMs.

| Raw folder | Pass | Fail |
|---|---:|---:|
| `out_3_shot_frama-c` | 28 | 13 |
| `out_FIB` | 277 | 136 |
| `out_SVCOMP` | 114 | 133 |
| `out_code2inv` | 89 | 34 |
| `out_framac` | 205 | 136 |
| `out_framac2` | 468 | 240 |

### Official annotated output validation

Fresh Frama-C/WP validation of AutoSpec's official annotated outputs:

| Status | Count |
|---|---:|
| `proved` | 192 |
| `unproved` | 21 |
| `annotation_error` | 19 |
| `unknown` | 18 |
| Total | 250 |

This validates that the AutoSpec artifact and Frama-C path are usable, but also
shows that the current container is not bit-for-bit identical to the paper
environment. The main known mismatch is Z3 `4.8.10` in the Docker image versus
AutoSpec's README-requested Z3 `4.8.6`.

### Fresh AutoSpec rerun

Fresh AutoSpec rerun on `official_251` using OpenRouter-compatible
`gpt-3.5-turbo`:

| Status | Count |
|---|---:|
| `pass` | 156 |
| `fail` | 92 |
| `missing` | 2 |
| `timeout` | 1 |
| Total | 251 |

By family:

| Family | Pass | Fail | Missing | Timeout |
|---|---:|---:|---:|---:|
| `code2inv_133` | 103 | 30 | 0 | 0 |
| `fib_46` | 20 | 26 | 0 | 0 |
| `frama_c_problems` | 23 | 25 | 2 | 1 |
| `svcomp` | 10 | 11 | 0 | 0 |

This is a provider-compatible reproduction, not a strict original-provider
reproduction, because it uses the available OpenRouter endpoint rather than a
pinned original OpenAI endpoint and exact historical model snapshot.

## BMC-Agent Native ACSL Results

### Matched 10-case pilot

| Method | Model | Result |
|---|---|---|
| AutoSpec | `gpt-3.5-turbo` via OpenRouter | 8 pass, 2 fail |
| AutoSpec | Claude via OpenRouter | 7 pass, 3 fail |
| BMC-Agent native ACSL | Claude via OpenRouter | 2 success, 5 unproved, 3 annotation_error |

### BMC-Agent stratified 50-case run

The 50-case run was selected to avoid overfitting to the easy scalar examples:

| Family | Count |
|---|---:|
| `code2inv_133` | 15 |
| `fib_46` | 10 |
| `frama_c_problems` | 15 |
| `svcomp` | 10 |

Result:

| Status | Count |
|---|---:|
| `success` | 4 |
| `unproved` | 19 |
| `annotation_error` | 27 |
| Total | 50 |

By family:

| Family | Success | Unproved | Annotation error |
|---|---:|---:|---:|
| `code2inv_133` | 0 | 2 | 13 |
| `fib_46` | 0 | 7 | 3 |
| `frama_c_problems` | 4 | 7 | 4 |
| `svcomp` | 0 | 3 | 7 |

Successful cases:

| Case | Goals |
|---|---:|
| `frama-c-problems/arrays_and_loops/1.c` | 10 / 10 |
| `frama-c-problems/general_wp_problems/max_of_2.c` | 15 / 15 |
| `frama-c-problems/pointers/add_pointers.c` | 11 / 11 |
| `frama-c-problems/pointers/swap.c` | 10 / 10 |

Interpretation:

- Native ACSL generation is functional.
- The current implementation is not competitive with AutoSpec on this ACSL/WP
  benchmark yet.
- The highest-value next fix is syntax and injection robustness, not a larger
  run. Many failures are Frama-C parser rejections, so expanding to 251 cases
  would mostly repeat the same failure mode.

## DSL vs ACSL Final-Outcome Equivalence

This experiment does not compare native ACSL generation quality. Instead, it
asks whether the original BMC-Agent DSL, when translated to the supported ACSL
subset and projected back into the original CBMC harness semantics, changes the
final BMC decision.

Stage A avoids LLM nondeterminism:

1. Load existing DSL `spec.json`.
2. Translate it to ACSL.
3. Project supported ACSL clauses back to normalized BMC-Agent specs.
4. Run both branches through the same CBMC harness path.
5. Compare final labels and confirmed-bug preservation.

Current Stage A summary:

| Metric | Result |
|---|---:|
| Total cases | 120 |
| Runnable cases | 81 |
| Interpretable cases | 55 |
| Final-label agreement on interpretable cases | 55 / 55 |
| Confirmed-bug preservation | 4 / 4 |
| Direct harness bug replay | 6 / 6 |
| Spec replay runnable | 49 / 75 |
| Projection-only support rate | 13 / 36 |

Important limitation:

This supports the claim that the supported DSL subset preserves final BMC
outcomes under replay. It does not prove that ACSL and the original DSL are
fully semantically equivalent. Unsupported clauses, projection-only cases, and
native ACSL generation failures are reported separately.

## Regression Check Against Original BMC Path

Targeted checks found no evidence that adding the optional ACSL commands changes
the original BMC-Agent behavior:

- Focused ACSL tests passed.
- Full suite failures were reproduced on clean `origin/main`, so they were not
  introduced by ACSL.
- Baseline finding counts matched `origin/main` on:
  - `examples/simple_driver.c`: 344 vs 344
  - `examples/sensor_hub.c`: 177 vs 177
  - `examples/block_device.c`: 264 vs 264
- Existing libarchive finding harnesses still reproduce CBMC failures.

## Recommended Next Step

Do not run BMC-Agent native ACSL on all 251 AutoSpec cases yet.

The 50-case result already answers the current decision question:

- AutoSpec is a usable public ACSL baseline.
- BMC-Agent can emit and evaluate native ACSL.
- The present native ACSL prompt/injection path is not mature enough for a
  meaningful full-scale comparison.

The next engineering target should be:

1. reject or repair malformed ACSL before Frama-C;
2. avoid invalid clauses such as nonsensical `\result` contracts on incompatible
   functions;
3. improve loop assigns/invariants for `code2inv` and `fib_46`;
4. rerun the same 50-case manifest before considering `official_251`.

## What This Branch Is Ready to Show

This branch is ready for review as an evidence package showing:

- how to run AutoSpec from its official artifact;
- what we reproduced locally;
- how BMC-Agent native ACSL is evaluated on the same benchmark format;
- why current BMC-Agent ACSL performance is below AutoSpec;
- why ACSL support appears isolated from the original DSL/CBMC pipeline.

It should not be presented as:

- a full AutoSpec paper replication;
- a claim that native ACSL generation is already competitive;
- a replacement for the original BMC-Agent DSL pipeline.
