# Reproduction guide

Use Python 3.12 and install the locked dependencies with `uv sync --frozen`. Run the commands from the repository root. The [artifact index](../../artifacts/README.md) links the recorded data for each research question.

The generation examples write a run plan with the input hashes, settings, and launch commands. To execute a plan, add `--execute` and set `OPENROUTER_API_KEY`, or specify an endpoint and credential variable with `--base-url` and `--api-key-env`. Use a fresh output directory for each run.

## RQ1. Verification benchmarks

The [RQ1 data](../../artifacts/rq1/README.md) provide the task outcomes and settings for both suites. Pass `--artifact-root .` to use the bundled benchmark files. The runner checks each input against the [input lock](benchmark_inputs.json).

Generate a plan for the AWS CBMC profile with CBMC 5.95.1:

~~~sh
uv run python experiments/paper_reproduction/run.py rq1-aws \
  --artifact-root . --cbmc-path /path/to/cbmc \
  --output artifacts/reproduction/aws
~~~

The LDV data record five ILP32 configurations over the same 29 inputs. The two BMC-Agent runs share one [saved configuration per task](../../artifacts/rq1/tier-b-ldv/results/agent_configurations.json). Their [run settings](../../artifacts/rq1/tier-b-ldv/results/run_settings.json) give the source revisions, solver versions, and resource limits. The [ESBMC backend guide](../../docs/ESBMC.md) documents backend selection.

To run the standalone LDV CBMC configuration with CBMC 5.95.1:

~~~sh
mkdir -p artifacts/reproduction/ldv-inputs
tar -xzf artifacts/rq1/sources/ldv-linux-3.4-simple-unsafe-tasks.tar.gz \
  -C artifacts/reproduction/ldv-inputs
uv run python artifacts/rq1/tier-b-ldv/runners/run_bare_cbmc.py \
  --inputs artifacts/reproduction/ldv-inputs --cbmc /path/to/cbmc \
  --out artifacts/reproduction/ldv-cbmc --unwind 1 \
  --timeout 600 --memory-kib 32000000 --workers 12
~~~

For standalone ESBMC and CPAchecker, set `ESBMC_ROOT` and `CPACHECKER_ROOT` to the ESBMC 8.3.0 and CPAchecker 4.2.2 installations. Set `SV_BENCHMARKS_ROOT` to the SV-Benchmarks checkout for `c/properties/unreach-call.prp`.

~~~sh
RQ1_LDV_TASK_ROOT="$PWD/artifacts/reproduction/ldv-inputs" \
  RQ1_TIER_B_BASELINE_RAW="$PWD/artifacts/reproduction/ldv-esbmc-cpa-raw" \
  RQ1_TIER_B_BASELINE_TSV="$PWD/artifacts/reproduction/ldv-esbmc-cpa.tsv" \
  bash artifacts/rq1/tier-b-ldv/runners/run_baselines.sh
~~~

## RQ3. Specification generation

Use [AutoSpec](https://zenodo.org/records/10912658) for the C inputs (`AutoSpec.zip` MD5 `d35646ade6c9bae8fdd48582e50d8567`) and [SpecGen-Artifact at `3b8355f`](https://github.com/Lezhi-Ma/SpecGen-Artifact/tree/3b8355f60807dbdd9019c26b39bfe0bad2ec96a6) for the Java inputs.

The ACSL profile uses Claude Sonnet 4.6, CBMC 6.9.0, and Frama-C/WP 26.0 with Alt-Ergo and Z3. Use the included Frama-C wrapper with Docker.

~~~sh
uv run python experiments/paper_reproduction/run.py rq3-acsl \
  --autospec-root /path/to/AutoSpec/benchmark \
  --cbmc-path /path/to/cbmc-6.9.0/bin/cbmc \
  --frama-c-path experiments/paper_reproduction/tools/frama-c \
  --output artifacts/reproduction/acsl
~~~

The four JML profiles use OpenJML `0.17.0-alpha-15` and its bundled JDK.

| Benchmark | Claude Sonnet 4.6 | GPT-3.5 Turbo 1106 |
| --- | --- | --- |
| SpecGenBench | `rq3-jml-specgen-claude` | `rq3-jml-specgen-gpt35` |
| SV-COMP Java | `rq3-jml-svcomp-claude` | `rq3-jml-svcomp-gpt35` |

~~~sh
uv run python experiments/paper_reproduction/run.py rq3-jml-specgen-claude \
  --specgen-root /path/to/SpecGen-Artifact \
  --openjml-path /path/to/SpecGen-Artifact/openjml/openjml \
  --output artifacts/reproduction/jml
~~~

## Replay accepted proofs

The bundle contains annotated programs and the flags used to check the ACSL and Claude JML proofs. Replay uses the verifier and the saved inputs, with zero model calls. Run the commands below to prepare the replay plans. Add `--execute` to run the verifiers.

~~~sh
mkdir -p artifacts/proof-bundle
tar -xzf artifacts/rq3/accepted-proofs-20261104.tar.gz -C artifacts/proof-bundle

uv run python experiments/paper_reproduction/replay_verifiers.py acsl \
  --manifest artifacts/proof-bundle/acsl_manifest.json \
  --tool experiments/paper_reproduction/tools/frama-c \
  --output artifacts/proof-replay/acsl

uv run python experiments/paper_reproduction/replay_verifiers.py jml \
  --manifest artifacts/proof-bundle/jml_manifest.json \
  --tool /path/to/SpecGen-Artifact/openjml/openjml \
  --output artifacts/proof-replay/jml
~~~
