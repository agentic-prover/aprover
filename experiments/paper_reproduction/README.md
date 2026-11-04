# BMC-Agent paper reproduction: RQ1 and RQ3

This branch provides one source checkout and seven explicit experiment profiles.
It is based on public commit `149177dead29443bcef96f8be194e2638d875fd9`, with the
recovered JML temperature/experiment patch and the recovered ACSL experiment
helpers. It does not switch production code to another checkout at run time.

The branch is a **reproduction candidate**. The targeted checks described below
do not establish that a new full run will reproduce every published count.
In particular, the original LDV code snapshot is unavailable and the original
GPT-3.5 model is unavailable in the OpenRouter catalog checked on 2026-11-04.

## Historical experiment mapping

Git dates below include the recorded UTC offset. The RQ1 log timestamps do not
encode a timezone and are reported verbatim. An artifact commit dates the
archive, not the code that generated it.

| Experiment | Execution / original archival evidence | Historical implementation | Confidence and reproduction status |
| --- | --- | --- | --- |
| RQ1 AWS, 27 tasks | Logs: 2026-06-29 03:23–05:19; private archive `15e3d34`, committed 2026-06-29 18:35 +04:00 | `4569fa1` (June 24) introduces the exact scope-from-entry CLI marker found in all 27 logs. Original runner uses `/home/syc/AProver_cegar`. | Medium confidence in the code family; exact SHA/local patch unrecorded. This branch fixes LP64, unreach-call, unwind 64, solver 400 s, task 800 s. |
| RQ1 LDV, 29 tasks | 13 saved attempts on mousedev: 2026-06-28 14:41–17:37; same June 29 archive | Private `AProver_cegar` checkout, including lock-writer CEGAR changes. Public `44a3cbf`/`149177d` on July 5 provide a later compatible frame-havoc implementation. | No exact public revision established. Do not label July 5 as the June 28 execution version. This branch fixes ILP32, unwind 1, solver 300 s, task 900 s, manual lean frame-havoc. |
| RQ3 ACSL, 251 cases, five trials | Private archive `7210967`, 2026-06-23 15:21 +04:00; explicit toolchain metadata | `1528d0dcd9faecbc5b3fd40dc203aec38453af89`, plus untracked batch/base/postcheck helpers recovered locally | Explicit core SHA; helpers are now tracked and portable. All 251 current inputs match both the public AutoSpec archive and the original run's saved attempt-1 sources. |
| RQ3 JML, two suites × two models, ten trials | Private archive `9bc9a1c`, 2026-06-23 14:47 +04:00; original worktree, prompt/cache evidence | `fcad091a344645f1e4930f31ce7b63507653187e` plus local patch, including temperature 0.4 | High-confidence reconstructed snapshot. The JML module and runner are byte-identical between that base and `149177d` before applying the same patch. Claude route checked with fresh trials; GPT-3.5-1106 requires the original model to be available at the chosen endpoint. |

The anonymized RQ3 bundle was committed on June 30 (`d29c25e`) and RQ1 packaging
on July 8 (`4b38c1b`). These dates should not be used to select runtime code.
Older private paths call the current paper's RQ3 `rq2`; this is an artifact
numbering difference.

## What the module comparison establishes

* Between ACSL `1528d0d` and this branch's base, `assert_driven_specs.py`,
  `frama_c.py` and `acsl.py` are byte-identical. The only changed function in
  `loop_invariants.py` is `build_havoc_abstraction`; the Frama-C/WP loop oracle
  does not use that CBMC abstraction path. The relevant CLI specs-bench handlers
  are unchanged. Other shared helpers still require attention when generalizing
  beyond the checked inputs.
* The JML source and experiment runner are byte-identical from `fcad091` through
  `149177d`. The LLM client changes in this interval add an unused Codex provider;
  the OpenAI-compatible transport used by these runs is unchanged.
* RQ1 frame-havoc and faithful-main modes, model routing, architecture, property
  selection and budgets are fixed by the profiles. PlanAgent is not invoked;
  this prevents a later default plan from silently replacing those parameters.
* Commit titles alone are insufficient. `221c611` is titled “web: revamp
  implementation” but also changes the pipeline, LLM client, engine, validator,
  preprocessor and specification generator. Later planning, triage and unwind
  changes can affect RQ1, even if Java/JML generation remains unchanged.

The complete local history audit is produced by `audit_history.py` below.
Module equality supports compatibility; it is not a proof that all possible
inputs, model responses and verifier environments give identical outcomes.

## Fixed profiles

| Profile | Inputs | Default model | Trials |
| --- | ---: | --- | ---: |
| `rq1-aws` | 27 | Claude Sonnet 4.5 | 1 |
| `rq1-ldv` | 29 | Claude Sonnet 4.5 | 1 |
| `rq3-acsl` | 251 | Claude Sonnet 4.6 | 5 |
| `rq3-jml-specgen-claude` | 120 | Claude Sonnet 4.6 | 10 |
| `rq3-jml-specgen-gpt35` | 120 | GPT-3.5 Turbo 1106 | 10 |
| `rq3-jml-svcomp-claude` | 265 | Claude Sonnet 4.6 | 10 |
| `rq3-jml-svcomp-gpt35` | 265 | GPT-3.5 Turbo 1106 | 10 |

JML uses temperature 0.4, `specgen-4shot`, at most ten generation/refinement
iterations, and a 200 s OpenJML timeout. ACSL preserves the original five-trial
runner and WP postcheck: `-wp-precond-weakening -wp-no-callee-precond`,
`Alt-Ergo,Z3`, 8 s per prover goal, 180 s postcheck timeout. Its generation path
uses the original default temperature 0.2 and four prompt examples.

`benchmark_inputs.json` freezes SHA-256 for every selected input. The runner
rejects a changed input instead of silently evaluating another benchmark.
Ambient `BMC_*`, `AMC_*`, `SVCOMP_*` and role-routing overrides are removed from
the child environment before explicit profile settings are applied.

## Environment

Use Python 3.12 and the repository's dependencies (`uv sync --frozen`).
The following tool versions are checked before execution:

* RQ1: CBMC 5.95.1.
* ACSL: CBMC 6.9.0, Frama-C/WP 26.0 with Alt-Ergo and Z3.
* JML: OpenJML `0.17.0-alpha-15` and its bundled JDK.

The checked ACSL container was `framac/frama-c:26.0.debian`, image ID
`sha256:afbfbc34a0974e83b54b7db90ee381b8c05fbab7f94bac010a112b30c56aeb00`,
repository digest
`framac/frama-c@sha256:bda451103345fdd1e2e57262bf3c6ff36090ce819a03c163b24f637d4dd8e053`.
The included `tools/frama-c` wrapper pins this digest and mounts the current
checkout as the container's working directory. The scripts create temporary
verifier files inside the checkout.

Provide keys through environment variables. The default endpoint is OpenRouter
and reads `OPENROUTER_API_KEY`. For a direct OpenAI endpoint, use
`--base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY` and the
unnamespaced model ID, e.g. `--model gpt-3.5-turbo-1106`. This does not imply that
the historical model is currently served there. A different model is a new
experiment and must be reported as such.

## Plan or execute

Every command below writes a plan without running a model or verifier. Append
`--execute` to actually run. `--cases`, `--limit` and `--trials` can select a
pilot; pilots are labeled explicitly. Use a new output directory for each run.

```sh
# Run from the reproduction checkout after installing dependencies.
python experiments/paper_reproduction/run.py rq1-aws \
  --artifact-root /path/to/BMC_ICSE \
  --cbmc-path /path/to/cbmc-5.95.1/bin/cbmc \
  --output artifacts/reproduction/rq1-aws

python experiments/paper_reproduction/run.py rq1-ldv \
  --artifact-root /path/to/BMC_ICSE \
  --cbmc-path /path/to/cbmc-5.95.1/bin/cbmc \
  --output artifacts/reproduction/rq1-ldv

python experiments/paper_reproduction/run.py rq3-acsl \
  --autospec-root /path/to/AutoSpec/benchmark \
  --cbmc-path /path/to/cbmc-6.9.0/bin/cbmc \
  --frama-c-path experiments/paper_reproduction/tools/frama-c \
  --workers 4 --output artifacts/reproduction/rq3-acsl

python experiments/paper_reproduction/run.py rq3-jml-specgen-claude \
  --specgen-root /path/to/SpecGen-Artifact \
  --openjml-path /path/to/SpecGen-Artifact/openjml/openjml \
  --workers 4 --output artifacts/reproduction/rq3-jml-specgen-claude
```

Use the other three JML profile names for the remaining suite/model combinations.
RQ1 inputs come from the exact bundled task archives, not the machine's current
SV-COMP checkout. The archives are extracted with path/link checks. The bundled
LDV sources contain an anonymized diagnostic file-path literal, which is recorded
in the local public-input provenance check.

Each execution writes `run_manifest.json` (commit, source hashes, input hashes,
explicit environment, tool versions and commands), `execution.json`, logs,
solver artifacts, and `metrics.json`. Existing outputs with a different
code/configuration fingerprint are rejected. Execution timeout or an empty
report list is not interpreted as safety. Frame-havoc counterexamples are
reported separately as abstract candidates, with pipeline report counts retained.

## Model accounting and controlled replay

To record actual token/cost usage and bound a small pilot:

```sh
python experiments/paper_reproduction/recording_proxy.py \
  --output artifacts/pilot/recording --budget-usd 8 --max-calls 300 --port 8794
```

Run a profile with
`--base-url http://127.0.0.1:8794/openrouter/unique_run_tag/api/v1` and
`--call-log artifacts/pilot/recording/llm_calls.jsonl`. Give every independent run
a unique tag. The loopback recorder forwards only to OpenRouter, preserves
model/prompt/temperature/token-cap settings, adds usage accounting, and saves
request/response bodies without authorization headers. The spending guard reserves
a conservative price bound before sending each call. Unknown model IDs are not
silently replaced.

`replay_proxy.py` can replay those responses with no external requests. It matches
the complete request body; optional `--normalize-prefix` values normalize only
explicitly listed machine-local output paths. Replay calls and fresh calls must
be reported separately. Replay is useful for checking code compatibility with
the model output held fixed, not for estimating current model performance.

## Checks completed on 2026-11-04

* 362 relevant unit tests passed, one skipped.
* Full task discovery: 27 AWS, 29 LDV, 251 ACSL, 120 SpecGenBench and 265 SV-COMP
  Java inputs; five/ten-trial defaults are preserved.
* Fresh one-trial runs: ACSL `arrays_and_loops/1.c` (WP 3/3) and Code2Inv `1.c`
  (WP 10/10); JML `AbsSeq` and `Addition` (clean OpenJML passes).
* A historical `1528d0d` control, with the same recovered experiment adapters and
  JML temperature patch, received the recorded model responses. All four final
  verification inputs were byte-identical and verifier results agreed. The
  non-loop ACSL case required explicit output-path normalization for cache matching;
  the other three cases matched complete request bodies directly.
* RQ1: `aws_add_size_saturating_harness` produced a bounded no-violation result;
  `aws_array_eq_c_str_harness-1` rediscovered `reach_error` and retained one
  pipeline report. LDV `kernelcapi` produced an abstract `reach_error` candidate
  and two pipeline reports; this is not a faithful-program witness proof.
* The seven fresh cases made 145 model calls, using 794,129 prompt tokens and
  36,532 completion tokens. OpenRouter's actual billed usage was $2.57974665,
  matching the recorder exactly. Historical response replays added no API cost.
* No full benchmark was rerun. These checks target module compatibility and
  executable packaging, not a new estimate of the paper's aggregate scores.

The local evidence is in `artifacts/audit_20261104/history.json`,
`artifacts/audit_20261104/benchmark_input_check.json`, and
`artifacts/pilot_20261104/paired_comparison.json`. Pilot logs and billing records
are under `artifacts/pilot_20261104/` and are intentionally excluded from Git.

To repeat the history audit against available local clones:

```sh
python experiments/paper_reproduction/audit_history.py \
  --aprover /path/to/aprover \
  --private-artifacts /path/to/aprover-findings-embargoed \
  --anonymous-artifacts /path/to/BMC_ICSE \
  --output artifacts/audit/history.json
```

Private remote refresh could not be performed in this session (GitHub connector
404; SSH authentication denied). The audit uses the available private clone at
`1d12b534908cf3cd81cc722859229afba9666a3c`, including its reachable branch history.
Public AProver main was checked at `b6ac080`; it changes only README relative to
`b9314c7`, so those two commits have identical pipeline code.

## Remaining decisions

1. Recover the original remote LDV CEGAR changes or evaluate this explicit public
   replacement and update the RQ1 numbers. A previous public `149177d` plan rerun
   caught 14/29, not the paper's 18/29; its artifact TSV also contains 14 `CATCH`
   rows. Do not explain the discrepancy solely by unrelated commits.
2. Resolve LDV's paper LP64 statement versus the artifact's ILP32 configuration.
   This candidate follows the artifact and records ILP32 explicitly.
3. Decide how to handle unavailable GPT-3.5-1106. Preserve the historical result
   and environment description, or rerun a matched comparison using an available
   model; do not relabel a substitute model as the original experiment.
4. After these decisions, run only the full experiments whose outcomes are needed
   to validate the chosen release and paper claims. Pilot compatibility alone is
   not a basis for asserting exact aggregate reproduction.

## Public benchmark sources

* AutoSpec: <https://zenodo.org/records/10912658>, `AutoSpec.zip` MD5
  `d35646ade6c9bae8fdd48582e50d8567`.
* SpecGen: <https://github.com/Lezhi-Ma/SpecGen-Artifact>, revision
  `3b8355f60807dbdd9019c26b39bfe0bad2ec96a6`.
* SV-COMP: <https://github.com/sosy-lab/sv-benchmarks>, revision
  `2e1723fde6aa65a250dcb677efa45edaa4b6b631`; the reproduction inputs are frozen
  independently by SHA-256.
