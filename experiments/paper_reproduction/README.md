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
| RQ1 LDV, 29 tasks | 13 saved attempts on mousedev: 2026-06-28 14:41–17:37; same June 29 archive | Private `AProver_cegar` checkout, including global-write frame summaries and lock-writer CEGAR changes. Public `44a3cbf`/`149177d` on July 5 do not establish an equivalent implementation. | Original implementation remains missing. The `rq1-ldv` profile is explicitly a public fallback, preserving the archived environment and ILP32/unwind 1/300 s solver/900 s task budgets; it is not the historical algorithm. |
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
* The other `SpecGeneratorV2` changes between `1528d0d` and `149177d` are a
  kernel/transitive-inline short circuit and buffer-extent canonicalization in
  split-precondition generation. The ACSL profile clears transitive-inline
  overrides. Its original `--agentic` preset enables split generation, so the
  later canonicalization would otherwise be active: the explicit
  `canonicalize_spec_buffer_extents=False` profile restores the original return
  of the contract-only precondition without tightening `n + k` to `n`.
  `spec_evidence.py` and the caller-grounded spec prompt function are unchanged.
  All seven functions in the recovered WP postcheck helper are AST-identical to
  the original helper; the batch helper changes only its helper location and
  Python invocation.
* Two other shared changes reach assertion-driven synthesis: DSL clause filtering
  and the later kernel-TU/main harness rules. The ACSL profile explicitly selects
  `dsl_filter_policy=2026-06-22` and `c_harness_policy=2026-06-22`. With these
  settings, all 220 recorded conditions translate identically to June 22, and
  215 captured engine queries from 110 archived trials / 22 assertion cases have
  identical harnesses and solver parameters after replacing only temporary TU
  names and output paths. Of these queries, 147 pass the final-contract
  C-expression gate; the rest were captured as hypothetical engine checks.
  These comparisons hold final recorded contracts fixed, rather than replaying
  every intermediate generation/refinement decision. Ordinary verification keeps
  the current DSL and harness defaults.
* RQ1 faithful-main and public LDV fallback settings, model routing, architecture,
  property selection and budgets are fixed by the profiles. PlanAgent is not invoked;
  this prevents a later default plan from silently replacing those parameters.
* Commit titles alone are insufficient. `221c611` is titled “web: revamp
  implementation” but also changes the pipeline, LLM client, engine, validator,
  preprocessor and specification generator. Later planning, triage and unwind
  changes can affect RQ1, even if Java/JML generation remains unchanged.

The public LDV implementation needs particular care. `BMC_FRAME_HAVOC` skips
specification generation; the harness does not implement the archived global-write
frame summaries or lock-writer refinement loop. In addition,
`BMC_TRANSITIVE_INLINE=0` is a nonempty string and therefore enables transitive
inlining in this code. The fallback preserves this actual behavior explicitly;
it does not silently reinterpret the archived environment as a different strategy.
Searching reachable histories in 22 local repositories (30 distinct reference
heads) and 40 cached bytecode files did not recover the missing LDV implementation.

The complete local history audit is produced by `audit_history.py` below.
Module equality supports compatibility; it is not a proof that all possible
inputs, model responses and verifier environments give identical outcomes.

`capture_acsl_queries.py --checkout /path/to/checkout --recorded-root
/path/to/original/cases --output /path/to/new/capture` repeats the assertion-engine
input capture against either checkout. It intercepts CBMC after the harness is
written and forbids model calls. Its synthetic successful return only advances
input capture and is never verifier evidence. Compare the resulting `queries.json`
rows and normalized harnesses; capture preserves all code and conditions.

## Fixed profiles

| Profile | Inputs | Default model | Trials |
| --- | ---: | --- | ---: |
| `rq1-aws` | 27 | Claude Sonnet 4.5 | 1 |
| `rq1-ldv` (public fallback; historical match unestablished) | 29 | Claude Sonnet 4.5 | 1 |
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

The runner fixes `PYTHONHASHSEED=0`: unsorted callee sets otherwise change the
order of contract summaries in model prompts across identical source checkouts.
The original runs did not record this seed. RQ1 uses both an unwind floor and cap
to enforce the listed solver bound. AWS retains the original agent-facing default
of 4 (the actual solver bound is 64); LDV sets the configuration and solver bound
to 1. A floor alone does not enforce unwind 1 because the ordinary configuration
default is 4. Solver timeout values remain per-call minima as in the archived
runner; the outer task timeout is the hard wall-clock budget.

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
solver artifacts, and `metrics.json`. `cbmc_invocations.jsonl` records the actual
solver argv and harness hash through a transparent exec wrapper. RQ1 execution
returns an error if those arguments do not match the profile's unwind and data
model. Existing outputs with a different
code/configuration fingerprint are rejected. Execution timeout or an empty
report list is not interpreted as safety. LDV fallback counterexamples are
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
Pass `--replay --replay-log /path/to/replay_calls.jsonl` to the run command.
Any cache miss invalidates the controlled replay, even if the pipeline subsequently
finishes using fallback specifications or classifications.
The opt-in `--normalize-diagnostic-identifiers` proxy option also normalizes ASan
diagnostic PIDs and temporary C filenames in compiler diagnostics. Program values,
source code, diagnostic categories and diagnostic text remain part of matching.
The replay log records whether this option was enabled.

## Checks completed on 2026-11-04

* 465 relevant unit tests passed, one skipped, including C harness regressions.
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
  and two pipeline reports before the exact-unwind correction (its configuration
  still used the default 4). This does not validate the corrected unwind-1 profile
  and is not a faithful-program witness proof.
* The seven fresh cases made 145 model calls, using 794,129 prompt tokens and
  36,532 completion tokens. OpenRouter's actual billed usage was $2.57974665,
  matching the recorder exactly. Historical response replays added no API cost.
* No full fresh-generation benchmark was rerun. These checks target compatibility and
  executable packaging, not a new estimate of the paper's aggregate scores.
* A per-trial evidence audit recomputes the recorded RQ3 totals exactly: ACSL
  209/251 cases and 990/1255 trials; Claude JML 112/120 and 1057/1200 on
  SpecGenBench, 227/265 and 2172/2650 on SV-COMP. All passing ACSL and Claude/JML
  records have their verification evidence, final inputs and matching recorded
  source text. GPT work is deferred; the optional stricter audit of its files
  found six source-text mismatches and four missing failed-trial JSON records,
  retained separately for later investigation.
* Three additional archived JML proofs (`BinarySearch`, `SelectionSortDesc`,
  `assert3`) pass fresh OpenJML execution after relocation, including the SV-COMP
  support source. Three archived ACSL proofs (`fib_46/26`, mutable-array doubling,
  and the large-constant SV-COMP case) also pass fresh Frama-C execution with the
  original goal counts. These six verifier replays make zero model calls.
* A fixed historical `mousedev` harness still timed out after 300 s under ILP32
  and exact unwind 1. Correcting the data model alone did not recover this
  historical catch. The prior full public `--plan` rerun used LP64, while its
  no-plan counterpart retained at least unwind 4 and an unrecognized property
  path instead of the required `unreach` token; neither is a full run of the
  corrected profile.
* Complete ACSL proof validation passed for all 378 distinct accepted inputs,
  covering all 990 recorded successful trials and 209 cases. Every WP goal count
  matches the corresponding original record. This validates archived accepted
  proofs, rather than a fresh 1,255-trial generation experiment.
* Complete Claude/JML proof validation passed for all 932 distinct accepted input
  bundles: 1,057 successful trials / 112 cases on SpecGenBench and 2,172 trials /
  227 cases on SV-COMP. All 3,229 accepted source files also match the successful
  annotated-source text recorded in their original trial JSON. These checks make
  zero model calls and do not re-estimate generation rates.
* After fixing the Python hash seed, two fresh AWS cases again produced one
  bounded no-violation result and one `reach_error` result with one pipeline
  report. All three actual solver invocations use unwind 64 and LP64. Their
  controlled replay matches all 86 cached requests after explicit output-path
  and diagnostic-identifier normalization; both final harnesses are byte-identical.
  The 86 fresh calls cost $1.17823845, exactly matching the official key usage
  increment. Together with the earlier pilot, measured fresh-call cost is
  $3.75798510. Failed replay attempts remain in the audit artifacts and are not
  compatibility evidence.
* Checker-only replay covers all 27 exact AWS inputs, with no model calls:
  17 verifier successes, seven `reach_error` counterexamples and three unknowns.
  `memset_using_uint64_harness` fails an unwinding assertion at bound 64. Its
  original log also records `verified=False`, despite a `true` scoreboard row.
  Counting no `reach_error` within bound 64 instead gives 18/7/2, matching all
  27 archived rows. The collector retains both metrics and does not turn
  incomplete unwinding into a safe proof. This is solver replay, not a full
  rerun of the LLM validation/filtering pipeline.

The AWS reproducer-tool transcript contains an ASan shadow-memory reservation
failure under the task's address-space limit, which the old tool labeled
`OK: reproduced SIGABRT`. That invocation is an infrastructure failure and is not
runtime confirmation of a program defect. The AWS result above is supported by
its faithful CBMC `reach_error` counterexample; no runtime-confirmation claim is
made from that diagnostic.

The local evidence is in `artifacts/audit_20261104/history.json`,
`artifacts/audit_20261104/benchmark_input_check.json`, and
`artifacts/pilot_20261104/paired_comparison.json`. Pilot logs and billing records
are under `artifacts/pilot_20261104/` and `artifacts/recovery_20261104/`,
intentionally excluded from Git. The latter also contains complete accepted-proof
replays, the 27-task AWS scoring audit, and paired ACSL translation/harness checks.

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

## Audit recorded RQ3 results and replay verification

The audit checks each original trial, its accepted proof log/result, and its saved
verification input. It returns a nonzero status for a count mismatch or missing
passing evidence, and emits hash-locked, deduplicated verifier manifests (378 C
inputs and 932 Claude/Java input bundles). The default `--model-scope claude`
follows the active scope; `--model-scope all` also checks the deferred GPT records.

```sh
python experiments/paper_reproduction/audit_published.py \
  --jml-runs-root /path/to/aprover_jml_optimize/artifacts \
  --acsl-run /path/to/original/autospec_251_bmc_agent_acsl_rerun_20260622 \
  --artifact-root /path/to/BMC_ICSE --output artifacts/recorded-audit

python experiments/paper_reproduction/replay_verifiers.py jml \
  --manifest artifacts/recorded-audit/jml_accepted_replay_manifest.json \
  --tool /path/to/SpecGen-Artifact/openjml/openjml \
  --run-contains claude46 --cases BinarySearch SelectionSortDesc assert3 \
  --one-per-case --output artifacts/proof-replay/jml --execute

python experiments/paper_reproduction/replay_verifiers.py acsl \
  --manifest artifacts/recorded-audit/acsl_accepted_replay_manifest.json \
  --tool experiments/paper_reproduction/tools/frama-c \
  --limit 3 --output artifacts/proof-replay/acsl --execute
```

Replay copies sources to the new output directory, verifies their hashes, checks
the historical tool version, and preserves the original verifier flags. It never
modifies the original artifacts or calls a model. Replaying accepted proofs does
not reproduce generation success rates or replace a full fresh RQ3 experiment.

Once complete proof validation has finished, `package_proofs.py` exports the
validated sources, flags, hashes and original trial coverage in a portable archive:

```sh
python experiments/paper_reproduction/package_proofs.py \
  --acsl-run artifacts/proof-validation/acsl \
  --jml-run artifacts/proof-validation/claude-jml \
  --output artifacts/rq3-accepted-proofs.tar.gz
```

The packager rejects incomplete or failed validation runs. The exported manifests
use paths relative to the bundle, so the original experiment directories are not
needed for subsequent solver replay. Historical failed trials and model generation
are outside this accepted-proof bundle's coverage.

## Remaining decisions

1. Recover the original LDV frame-summary/CEGAR changes or evaluate the
   explicitly different public fallback and update the RQ1 numbers. The owner
   does not know of a surviving checkout. A previous public `149177d` plan rerun
   contains 14/29 raw `reach_error` candidates, including two timed-out processes;
   the original artifact TSV also contains 14 `CATCH` rows. Neither establishes
   the paper's 18 concrete witnesses. Do not union catches from different runs.
2. Resolve LDV's paper LP64 statement versus the artifact's ILP32 configuration.
   This candidate follows the artifact and records ILP32 explicitly.
3. Resolve AWS's safe-proof versus bounded-no-violation wording and align the
   baseline's unwinding-assertion policy and budgets. The paper's 18/7/2 count
   includes the incomplete-unwinding `memset` row; strict replay gives 17/7/3.
4. GPT work is deferred. Eventually decide how to handle unavailable GPT-3.5-1106. Preserve the historical result
   and environment description, or rerun a matched comparison using an available
   model; do not relabel a substitute model as the original experiment.
5. After these decisions, run only the full experiments whose outcomes are needed
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
