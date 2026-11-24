# C/ACSL on AutoSpec

The evaluation uses 251 C programs and five trials per case. Frama-C/WP checks the generated ACSL annotations and the benchmark obligations.

## Result Summary

| Method | Benchmark | Model | Case pass | Trial pass |
|---|---|---:|---:|---:|
| AutoSpec paper | AutoSpec 251 | published baseline | 199/251 | - |
| AutoSpec | AutoSpec 251 | Claude Sonnet 4.6 | 219/251 | 1036/1255 |
| BMC-Agent | AutoSpec 251 | Claude Sonnet 4.6 | 209/251 | 990/1255 |

`Case pass` means at least one trial generated ACSL that let Frama-C/WP prove all benchmark goals. `Trial pass` counts all passing trials across all cases.

The [per-benchmark comparison](per_benchmark_comparison.csv), [per-case BMC-Agent results](per_case_bmc_agent_claude.csv), and trial reports for [AutoSpec](autospec/autospec_251/claude_sonnet_4_6/report.json) and [BMC-Agent](bmc_agent/autospec_251/claude_sonnet_4_6/report.json) provide the outcome data behind the table.

See the
[reproduction guide](../../bmcagent/experiments/paper_reproduction/README.md#rq3-specification-generation)
for benchmark inputs and commands.
