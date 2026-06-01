# Native ACSL Spec-Quality Benchmark

Date: 2026-05-31

This directory now contains an evaluation adapter for ACSL/Frama-C-style
spec-quality comparison. It is intentionally separate from the normal
BMC-Agent CBMC/Kani pipeline.

## Commands

Preflight artifact and toolchain status:

```bash
uv run python experiments/spec_quality_compare/spec_quality_benchmark.py discover \
  --output artifacts/spec_quality_benchmark/discovery.json
```

Import AutoSpec metadata after placing the official Zenodo zip at
`/mnt/disk7/jw_bmc/spec_quality_data/AutoSpec.zip`:

```bash
uv run python experiments/spec_quality_compare/spec_quality_benchmark.py import-autospec
```

Build and run the 4-case pilot:

```bash
uv run python experiments/spec_quality_compare/spec_quality_benchmark.py select-pilot \
  --size 4 \
  --output artifacts/spec_quality_benchmark/pilot4_manifest.json

uv run python experiments/spec_quality_compare/spec_quality_benchmark.py run \
  --manifest artifacts/spec_quality_benchmark/pilot4_manifest.json \
  --output artifacts/spec_quality_benchmark/pilot4_run
```

Aggregate one or more runs:

```bash
uv run python experiments/spec_quality_compare/spec_quality_benchmark.py aggregate \
  artifacts/spec_quality_benchmark/pilot4_run \
  --output artifacts/spec_quality_benchmark/pilot4_aggregate
```

Use `--allow-llm` only when intentionally spending OpenRouter/Claude calls.
Use `screen` for AutoSpec downloads, full Frama-C runs, or 10-case expansion.

## Current Smoke

Artifacts from the initial smoke:

- `artifacts/spec_quality_benchmark/discovery.json`
- `artifacts/spec_quality_benchmark/pilot4_manifest.json`
- `artifacts/spec_quality_benchmark/pilot4_run/run_report.json`
- `artifacts/spec_quality_benchmark/pilot4_run/summary.md`
- `artifacts/spec_quality_benchmark/pilot4_aggregate/report.json`

Current toolchain status is `frama_c_unavailable`, so static ACSL rows are
reported as preflight-blocked rather than failed. The `ncdev_bar_read`
witness-preservation fixture runs without Frama-C and currently demonstrates
the overconstraint warning path.

## Interpretation Rules

- AutoSpec rows are replication rows only after importing the official Zenodo
  artifact and using a compatible Frama-C/WP toolchain.
- SpecSyn rows remain "SpecSyn-inspired" unless an official artifact is found.
- SpecGen is related metric context in this v1 because it targets Java/JML and
  OpenJML, not C/ACSL and Frama-C/WP.
