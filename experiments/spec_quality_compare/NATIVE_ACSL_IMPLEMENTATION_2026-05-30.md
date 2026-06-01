# Native ACSL Spec-Quality Implementation

Date: 2026-05-30

Question: can this branch evaluate BMC-Agent specs as native ACSL artifacts,
without relying on the older DSL-to-ACSL pilot path?

Answer: yes. The branch now has a native ACSL artifact path and a quality runner
that reports Frama-C/WP validity, clause counts, vacuity warnings, manual
coverage labels, and SpecSyn-style mutation/VDR when a mutation file is supplied.

## New Commands

Generate or load native ACSL specs and write annotated C:

```bash
uv run bmc-agent acsl-generate \
  --source experiments/spec_quality_compare/read_at.c \
  --driver native_read_at_strong_smoke \
  --output artifacts/spec_quality_compare_native \
  --spec-json experiments/spec_quality_compare/read_at_native_strong_acsl.json \
  --function read_at \
  --no-run-frama-c
```

Evaluate native ACSL specs:

```bash
uv run bmc-agent acsl-quality \
  --source experiments/spec_quality_compare/read_at.c \
  --driver native_read_at_strong_smoke \
  --output artifacts/spec_quality_compare_native \
  --spec-json experiments/spec_quality_compare/read_at_native_strong_acsl.json \
  --function read_at \
  --mutation-json experiments/spec_quality_compare/read_at_mutations.json \
  --wp-timeout 10 \
  --timeout 120 \
  --cpus 2
```

## Artifact Shape

Native ACSL specs are JSON objects with:

- `function_name`
- `requires[]`
- `ensures[]`
- `assigns[]`
- `loop_invariants[]`
- `raw_acsl`
- `generation_metadata`

The primary module is `bmc_agent/acsl_native.py`. The older `bmc_agent/acsl.py`
remains the DSL-to-ACSL debug bridge and Frama-C/WP runner.

## Smoke Results

Strong pointer/bounds spec:

- source: `experiments/spec_quality_compare/read_at.c`
- spec: `experiments/spec_quality_compare/read_at_native_strong_acsl.json`
- report: `artifacts/spec_quality_compare_native/native_read_at_strong_smoke/acsl_quality/quality_report.json`
- Frama-C/WP: success, 4/4 goals
- Mutation/VDR: 3/3 mutants killed

Weak pointer/bounds spec:

- source: `experiments/spec_quality_compare/read_at.c`
- spec: `experiments/spec_quality_compare/read_at_native_weak_acsl.json`
- report: `artifacts/spec_quality_compare_native/native_read_at_weak_smoke/acsl_quality/quality_report.json`
- Frama-C/WP: success, 4/4 goals
- Mutation/VDR: 0/3 mutants killed

Interpretation: both specs are valid on the original program, but mutation/VDR
separates the strong behavioral spec from the weak `ensures \true` spec. This is
the core SpecSyn-style quality distinction we need before running larger ACSL
benchmarks.

## SpecSyn Artifact Discovery

The paper references public upstream source repositories and reports a
constructed 50-file benchmark, but the text does not include an official artifact
repository URL for SpecSyn itself. A web search on 2026-05-30 found the arXiv
page and secondary summaries, but no official code/benchmark artifact.

Until an official artifact is located, experiments should be labeled
"SpecSyn-inspired ACSL quality pilot" rather than "SpecSyn replication".
