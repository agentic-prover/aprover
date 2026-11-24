# Java/JML on SpecGenBench and SV-COMP

OpenJML checks the generated JML annotations on SpecGenBench and the SV-COMP Java subset. Each setting uses ten trials per case.

The archived JML generation metadata records the model, benchmark inputs, and
per-case outcomes but not the BMC-Agent source commit. The accepted-proof
replay validation used `b80281c55d5c541931418a0d61c6db16fb838100`.

## Result Summary

All rows use temperature `0.4`.

| Method | Benchmark | Model | Case pass | Trial pass |
|---|---|---:|---:|---:|
| SpecGen | SpecGenBench | Claude Sonnet 4.6 | 116/120 | 1019/1200 |
| SpecGen | SpecGenBench | GPT-3.5 Turbo 1106 | 109/120 | 834/1200 |
| SpecGen | SV-COMP Java | Claude Sonnet 4.6 | 245/265 | 2068/2650 |
| SpecGen | SV-COMP Java | GPT-3.5 Turbo 1106 | 212/265 | 1349/2650 |
| BMC-Agent | SpecGenBench | Claude Sonnet 4.6 | 112/120 | 1057/1200 |
| BMC-Agent | SpecGenBench | GPT-3.5 Turbo 1106 | 103/120 | 960/1200 |
| BMC-Agent | SV-COMP Java | Claude Sonnet 4.6 | 227/265 | 2172/2650 |
| BMC-Agent | SV-COMP Java | GPT-3.5 Turbo 1106 | 202/265 | 1839/2650 |

`Case pass` means at least one of the 10 trials passed OpenJML for that case.
`Trial pass` counts all passing trials across all cases.

The [result summary](results_summary.csv) gives the aggregate counts. The [SpecGen](specgen/) and [BMC-Agent](bmc_agent/) result directories contain per-setting manifests and trial outcomes.

## SpecGenBench Category Breakdown

The following table breaks down the 120 SpecGenBench cases using the author-provided category mapping.
Category names are normalized to the five categories used in the SpecGen paper: Sequential, Branched, Single-path Loop, Multi-path Loop, and Nested Loop.
Each cell reports `case pass; trial pass`.

| Category | Cases | SpecGen+Claude | BMC-Agent+Claude | SpecGen+GPT-3.5 | BMC-Agent+GPT-3.5 |
|---|---:|---:|---:|---:|---:|
| Sequential | 26 | 26/26 cases; 248/260 trials | 26/26 cases; 253/260 trials | 26/26 cases; 248/260 trials | 26/26 cases; 253/260 trials |
| Branched | 23 | 23/23 cases; 208/230 trials | 22/23 cases; 220/230 trials | 23/23 cases; 205/230 trials | 22/23 cases; 213/230 trials |
| Single-path Loop | 24 | 22/24 cases; 212/240 trials | 23/24 cases; 215/240 trials | 22/24 cases; 154/240 trials | 19/24 cases; 179/240 trials |
| Multi-path Loop | 26 | 26/26 cases; 204/260 trials | 25/26 cases; 225/260 trials | 24/26 cases; 144/260 trials | 22/26 cases; 184/260 trials |
| Nested Loop | 21 | 19/21 cases; 147/210 trials | 16/21 cases; 144/210 trials | 14/21 cases; 83/210 trials | 14/21 cases; 131/210 trials |
| Total | 120 | 116/120 cases; 1019/1200 trials | 112/120 cases; 1057/1200 trials | 109/120 cases; 834/1200 trials | 103/120 cases; 960/1200 trials |

See the
[reproduction guide](../../bmcagent/experiments/paper_reproduction/README.md#rq3-specification-generation)
for benchmark inputs and commands.
