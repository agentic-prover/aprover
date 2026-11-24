# VibeOS RQ2 source-tree ablation results

Date: 2026-11-23

## Scope

This batch contains the three OFAT ablation arms over the four VibeOS modules
used in RQ2:

- `no_dynamic`
- `no_realism`
- `fixed_config`

Every run used the complete source tree at `vibeos/kernel`, not the prior
copy-only staging layout. All four source SHA-256 values match the teacher's
archived June sources.

No contemporaneous OpenRouter Full arm was run in this batch. The three arms
are directly comparable to one another, but this directory alone cannot
measure each arm's delta against a Full run made under the same OpenRouter
route. The earlier corrected Full is Anthropic-direct and can only be used as
an external sanity reference.

## Fixed configuration

- BMC-Agent: `f2f31711e`
- VibeOS: `866bfb9`
- CBMC: 5.95.1
- Model: `anthropic/claude-sonnet-4.6`
- API path: OpenRouter Anthropic-native `/api/v1/messages`
- Upstream provider: forced to `Anthropic` with fallback disabled
- Source layout: complete `vibeos/kernel` tree
- Include roots: `vibeos/kernel`, `vibeos/kernel/libc`
- Per-function CBMC budget: 180 seconds
- Module timeout: 9,000 seconds
- LLM workers: one per module
- Parallel validation: disabled
- Phase-3e triage: disabled
- Reproducer agent, feedback loop, and spec refiner: enabled

Four modules ran concurrently; the three arms ran in three waves.

## Validity

All 12 runs completed successfully:

- Pipeline end observed: 12/12
- Reported and effective exit code: 0 for 12/12
- Complete source-tree boundary count: 222 for 12/12
- LLM calls: 4,310
- HTTP 200: 4,310
- Unexpected model responses: 0
- Unexpected upstream providers: 0
- Local proxy denials: 0
- Input tokens: 22,706,499
- Output tokens: 2,295,253
- Estimated cost: **$102.54829**
- OpenRouter account usage: `$102.548745`
- Sum of module wall times: 36,467 seconds
- Overall wall time: approximately 4 h 06 min

The OpenRouter key had a `$200` limit at completion and retained
`$97.451255`.

The runner's `usage.json` aggregation initially failed because the jq variable
name `module` is reserved. The raw API ledgers were unaffected and valid. The
runner has been corrected, and all 12 `usage.json` files were regenerated from
the ledgers.

## Aggregate results

Counts are against the RQ2 manual labels and the June Full manifest. A
`confirmed_exact` result is the same function and property ID;
`confirmed_loose` is the same function and property class. `Meaningful`
excludes unwind artifacts but retains known false positives in the survivor
count.

| Arm | CBMC verdicts produced | Failed before verdict | Meaningful survivors | June confirmed exact | June confirmed class | Conditional labels | Known FP re-emitted | Unlabeled | Final dynamic | Calls | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `no_dynamic` | 37/47 | 10 | 18 | 5/11 | 2 | 1 | 3 | 7 | 0 | 1,656 | $42.50 |
| `no_realism` | 38/47 | 9 | 11 | 1/11 | 0 | 0 | 0 | 10 | 6 | 1,944 | $42.60 |
| `fixed_config` | 34/47 | 13 | 6 | 2/11 | 0 | 1 | 1 | 2 | 5 | 710 | $17.45 |

The exact and loose confirmed columns count distinct June labels matched.
Survivor counts are report rows and can include the same internal-helper
property under multiple callers.

## Per-module meaningful survivors

| Arm | elf | fb | memory | string | Total |
|---|---:|---:|---:|---:|---:|
| `no_dynamic` | 10 | 3 | 1 | 4 | 18 |
| `no_realism` | 2 | 1 | 3 | 5 | 11 |
| `fixed_config` | 4 | 1 | 0 | 1 | 6 |

## Comparison with the staged-source pilot

The staged-source pilot previously produced 3, 9, and 1 meaningful survivors
for these arms. With the source-tree correction, this batch produced 18, 11,
and 6 respectively. That is a large shift, especially for `no_dynamic` and
`fixed_config`.

This change is not a pure source-layout effect because the model was also
moved from direct Anthropic to OpenRouter's Anthropic-native interface with the
upstream pinned to Anthropic. The model version and upstream inference provider
are the same, but the gateway remains a controlled difference.

## Interpretation

1. **The source layout materially changes the experiment.** The earlier
   staged-source results substantially understate what these ablations find.

2. **`no_dynamic` does not simply produce fewer findings.** It produces the
   most meaningful survivors (18), but all are static confirmations:
   `final_dynamic=0`. Turning replay off removes the dynamic evidence tier and
   changes refinement paths; it should not be summarized only as reduced
   report count.

3. **`no_realism` yields fewer matches to the June known-good set.** It keeps
   11 survivors and six dynamic confirmations, but only one exact confirmed
   label and no conditional labels. Ten survivors are unlabeled. This does not
   prove they are false positives because realism was disabled, but it makes
   the result set much harder to validate.

4. **`fixed_config` has the clearest coverage cost.** It produces only 34 CBMC
   verdicts and leaves 13 functions failing before a verdict, compared with 37
   and 10 for `no_dynamic` and 38 and 9 for `no_realism`. It also has the
   fewest meaningful survivors.

5. **This is not yet the final Full-versus-ablation table.** A Full run through
   the same OpenRouter route is still required before claiming component deltas
   relative to Full.

## Outputs

- Machine-readable metrics: `metrics.json`
- Per-run validity: `validity.tsv`
- Surviving findings and label matches: `final_findings.tsv`
- Raw run logs and artifacts: `runs/`
- API ledgers and request/response bodies: `proxies/`
