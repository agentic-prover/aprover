# ACSL vs DSL Final-Outcome Equivalence

This is an experiment adapter, not a production backend switch.

Question A: if the same BMC-Agent DSL spec is represented as ACSL and then
projected back into the supported harness semantics, does the final CBMC
outcome change?

Question B: if the spec generation path is changed to native ACSL, then
projected into the same CBMC harness semantics, does the final outcome change?

Stage A is the primary evidence because it fixes the original DSL `spec.json`
and makes no LLM calls. Stage B is explicitly opt-in with `--allow-llm`; it
measures an end-to-end generation change and must be reported separately.

Stage A primary path:

1. Load an existing DSL `spec.json`.
2. Translate it to ACSL with the existing `bmc_agent.acsl` pilot translator.
3. Project only supported ACSL clauses back to a normalized `Spec`.
4. Run the original DSL spec and the ACSL-projected spec through the same
   `BMCEngine` / CBMC harness generator.
5. Report final-label agreement, confirmed-bug preservation, harness diffs,
   unsupported clauses, and overconstraint warnings.

No LLM calls are made by Stage A.

Stage B path:

1. Run the normal DSL spec generator.
2. Run the native ACSL generator, or load a cached native ACSL artifact.
3. Project supported native ACSL `requires` / `ensures` into a normalized
   `Spec`.
4. Run both branches through the same `BMCEngine` / CBMC harness generator.
5. Report final-label agreement, projection support, overconstraint warnings,
   runtime, and LLM metadata without recording any secret values.

## Commands

Build a local pilot manifest:

```bash
uv run python experiments/acsl_dsl_outcome_compare/acsl_dsl_outcome_compare.py discover \
  --output artifacts/acsl_dsl_outcome_compare/pilot_manifest.json
```

Build a comprehensive manifest from local artifacts:

```bash
uv run python experiments/acsl_dsl_outcome_compare/acsl_dsl_outcome_compare.py discover \
  --comprehensive \
  --output artifacts/acsl_dsl_outcome_compare/comprehensive_manifest.json \
  --limit 140
```

Run a small paired replay:

```bash
uv run python experiments/acsl_dsl_outcome_compare/acsl_dsl_outcome_compare.py run \
  --manifest artifacts/acsl_dsl_outcome_compare/pilot_manifest.json \
  --output artifacts/acsl_dsl_outcome_compare/pilot_run \
  --timeout 30 \
  --unwind 4
```

Run the no-LLM Stage A replay:

```bash
uv run python experiments/acsl_dsl_outcome_compare/acsl_dsl_outcome_compare.py run-stage-a \
  --manifest artifacts/acsl_dsl_outcome_compare/comprehensive_manifest.json \
  --output artifacts/acsl_dsl_outcome_compare/stage_a_full \
  --timeout 60 \
  --unwind 4 \
  --workers 16
```

Run Stage B only when LLM/API use is intended:

```bash
uv run python experiments/acsl_dsl_outcome_compare/acsl_dsl_outcome_compare.py run-stage-b \
  --manifest artifacts/acsl_dsl_outcome_compare/comprehensive_manifest.json \
  --output artifacts/acsl_dsl_outcome_compare/stage_b_e2e \
  --allow-llm \
  --timeout 120 \
  --unwind 4 \
  --workers 2
```

Omitting `--allow-llm` on Stage B is a hard error.

Run only the synthetic spec-replay controls:

```bash
uv run python experiments/acsl_dsl_outcome_compare/acsl_dsl_outcome_compare.py run \
  --manifest artifacts/acsl_dsl_outcome_compare/pilot_manifest.json \
  --output artifacts/acsl_dsl_outcome_compare/synthetic_smoke \
  --case-id synthetic_max2_clean \
  --case-id synthetic_read_at_overconstraint \
  --timeout 30 \
  --unwind 4
```

Regenerate reports from an existing `report.json`:

```bash
uv run python experiments/acsl_dsl_outcome_compare/acsl_dsl_outcome_compare.py report \
  --input artifacts/acsl_dsl_outcome_compare/pilot_run/report.json
```

## Result Classes

- `same_decision`: DSL and ACSL replay produce the same final label.
- `acsl_missed_confirmed_bug`: DSL finds a known bug, ACSL replay does not.
- `acsl_overconstrained`: ACSL replay verifies clean while requires exclude a
  known/synthetic witness family.
- `dsl_only_timeout`: DSL replay times out but ACSL replay does not.
- `acsl_only_timeout`: ACSL replay times out but DSL replay does not.
- `unsupported_clause`: at least one DSL clause has no supported ACSL projection.
- `missing_source`: the artifact could not be paired with a source file.
- `projection_only`: spec translation/projection stress case with no source or
  harness replay.
- `direct_harness_control`: known finding replay with an existing harness; this
  is smoke coverage, not ACSL/DSL spec equivalence evidence.
- `harness_diff`: final labels differ and generated harnesses differ.
- `validation_diff`: reserved for a later dynamic-validation paired run.
- `inconclusive`: metadata-only or otherwise not interpretable.

Direct finding harness controls are included only as smoke coverage for known
finding replay. They are not counted as evidence that ACSL projection preserves
spec semantics because they do not consume a DSL `spec.json`.

## Current Full Stage A Artifact

Latest local full Stage A run:

- Report: `artifacts/acsl_dsl_outcome_compare/stage_a_full/report.json`
- Summary: `artifacts/acsl_dsl_outcome_compare/stage_a_full/summary.md`
- CSV: `artifacts/acsl_dsl_outcome_compare/stage_a_full/decision_table.csv`
- Log: `artifacts/acsl_dsl_outcome_compare/stage_a_full/run.log`

Interpretation rules:

- `final_label_agreement` excludes branch-level `error` results.
- `raw_final_label_agreement_including_errors` is diagnostic only.
- `spec_replay_confirmed_bug_preservation` uses confirmed bug cases where the
  DSL branch actually found a bug as its denominator.
- `projection_only` and `direct_harness_control` are separated from
  `spec_replay` and must not be counted as representation-equivalence evidence.
