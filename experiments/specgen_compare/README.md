# SpecGen Java/JML Benchmark Runner

This directory contains evaluation helpers for running BMC-Agent's Java/JML
specification-generation path on the SpecGen benchmark artifact.

The runner is intentionally outside the production verification pipeline.  It
uses:

- Java input programs from `SpecGenBench/common`
- BMC-Agent JML generation through the configured LLM provider
- OpenJML ESC as the verifier
- one report row per benchmark case

Example:

```bash
# Provide credentials through your normal secret manager or shell environment.
# Do not store API keys in this repository.
export BMC_AGENT_LLM_PROVIDER=openai
export BMC_AGENT_LLM_MODEL="${SPECGEN_MODEL:-gpt-3.5-turbo-1106}"
export BMC_AGENT_LLM_API_KEY="${OPENAI_API_KEY:?set OPENAI_API_KEY first}"
export BMC_AGENT_LLM_BASE_URL=https://api.openai.com/v1

export SPECGEN_BENCH_ROOT=/path/to/SpecGen-Artifact/benchmark/SpecGenBench/common
export SPECGEN_ORACLE_ROOT=/path/to/SpecGen-Artifact/benchmark/SpecGenBench/oracle
export BMC_AGENT_OPENJML_PATH=/path/to/openjml

uv run python experiments/specgen_compare/run_bmc_jml_specgen.py run \
  --bench-root "$SPECGEN_BENCH_ROOT" \
  --oracle-root "$SPECGEN_ORACLE_ROOT" \
  --openjml-path "$BMC_AGENT_OPENJML_PATH" \
  --cases Abs Return100 AddLoop \
  --output artifacts/specgen_jml_pilot \
  --max-iterations 5
```

Outputs:

- `manifest.json`: selected benchmark cases.
- `report.json`: per-case status and artifact paths.
- `summary.md`: compact table for inspection.
- `<output>/cases/<case>/...`: BMC-Agent JML/OpenJML artifacts.

Status meanings:

- `passed`: OpenJML produced no output and exited successfully.
- `verification_failed`: generated JML was syntactically usable but insufficient
  for OpenJML to prove all obligations.
- `annotation_error`: OpenJML rejected the generated annotations/source shape.
- `source_changed`: the LLM changed executable Java code; this is rejected before
  OpenJML is run.
- `tool_missing`, `timeout`, `tool_error`: infrastructure failures.
