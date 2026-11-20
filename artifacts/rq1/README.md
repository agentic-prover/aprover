# RQ1. Traditional verifiers

The evaluation uses the SV-COMP `unreach-call` property. See the [benchmark manifest](MANIFEST.md) for input archives, versions, and configurations.

## AWS C Common

The [task scoreboard](tier-a-aws/scoreboard.tsv) covers 27 LP64 tasks (20 safe and seven unsafe). The BMC-Agent and CBMC scores count bounded absence of a target violation on safe tasks at unwind 64. Per-tool verdicts and logs are in [results](tier-a-aws/results/) and [raw outputs](tier-a-aws/raw/).

| Tool | Safe tasks counted | False alarms | Unsafe tasks caught | Unknown |
| --- | ---: | ---: | ---: | ---: |
| BMC-Agent | 18 | 0 | 7 | 2 |
| CBMC | 18 | 0 | 7 | 2 |
| ESBMC | 13 | 3 | 5 | 6 |
| CPAchecker | 5 | 1 | 4 | 17 |

## Linux Driver Verification

The LDV evaluation uses ILP32 on the 29 inputs with expected verdict `false` in the original LP64 task definitions.

| Configuration | Catches | Unsound | Unknown | Per-task limits |
| --- | ---: | ---: | ---: | --- |
| BMC-Agent + CBMC | 18/29 | 0 | 11 | 2,700 s, 42,000,000 KiB |
| BMC-Agent + ESBMC | 15/29 | 0 | 14 | 2,700 s, 42,000,000 KiB |
| CPAchecker | 13/29 | 0 | 16 | 600 s, 32,000,000 KiB |
| ESBMC with k-induction | 10/29 | 1 | 18 | 600 s, 32,000,000 KiB |
| CBMC at unwind 1 | 10/29 | 0 | 19 | 600 s, 32,000,000 KiB |

*Catches* counts solver-confirmed targets for BMC-Agent and reported target violations for standalone verifiers. *Unsound* counts safe verdicts that conflict with confirmed ILP32 target reachability. *Unknown* counts the remaining tasks.

- [Agent outcomes](tier-b-ldv/results/agent_results.tsv) record each backend's solver-confirmed feasibility and dynamic replay status. Count rows with outcome `target_feasibility_confirmed` for each backend. Both runs used the [same saved per-task configurations](tier-b-ldv/results/agent_configurations.json).
- [Standalone CBMC outcomes](tier-b-ldv/results/baseline_cbmc.tsv) use `target_violation` for a reported violation. [Standalone ESBMC and CPAchecker outcomes](tier-b-ldv/results/baselines_esbmc_cpa.tsv) use `CATCH`. The archived task labels identify the corresponding [ESBMC and CPAchecker logs](tier-b-ldv/raw/baselines_raw.tar.gz).
- [Run settings](tier-b-ldv/results/run_settings.json) record solver versions, source revisions, and execution limits. See the [reproduction guide](../../experiments/paper_reproduction/README.md#rq1-verification-benchmarks) for standalone runner commands.
