# RQ2. Agentic verification on VibeOS

## Comparison with FM-Agent

| Tool | Candidates | Confirmed defects | Distinct confirmed functions |
| --- | ---: | ---: | ---: |
| FM-Agent | 120 | 20 | 20 |
| BMC-Agent | 43 | 21 | 13 |

The [manual review labels](manual-review.tsv) cover all 43 BMC-Agent reports and the manually reviewed FM-Agent records. The [FM-Agent candidate list](fm-agent-candidates.tsv) gives the original verdicts for all 120 candidates.

The [primary-class breakdown](confirmed-findings-by-class.tsv) records the report counts and function lists from the paper's classification table. Functions with confirmed reports in multiple classes appear in each applicable row.

## Component ablation

The [configuration totals](ablation/ablation_summary.tsv), [per-module scores](ablation/scores.tsv), and [report matches](ablation/reports.tsv) cover 47 functions in `elf`, `fb`, `memory`, and `string`.

The ablation uses [VibeOS `866bfb9`](https://github.com/kaansenol5/VibeOS/tree/866bfb9b7fc554b5b8e0939b4626bcf0416b8b19), [BMC-Agent `b9314c7`](https://github.com/agentic-prover/aprover/commit/b9314c704c1142d195ae43abde4ad9e03f84aaa8), Claude Sonnet 4.6, and CBMC 5.95.1. Each configuration ran once with LLM-generated specifications and dynamic replay disabled.

| Configuration | Reports | Known bugs | Previously rejected |
| --- | ---: | ---: | ---: |
| Full (`full`) | 16 | 6/11 | 2/9 |
| Triage as final filter (`norealism`) | 9 | 2/11 | 0/9 |
| Floor (`floor`) | 28 | 5/11 | 4/9 |

Full enables the realism audit. Triage as final filter disables the realism audit and uses triage for the final verdict. Floor disables domain-knowledge extraction, merged BMC configuration, realism audit, triage, feedback, specification refinement, the reproducer agent, and specification-generation tools. Floor retains the separate flag selector, inlining advisor, and agentic harness repair.

The report count includes retained real and latent reports after excluding unwinding artifacts. The [manual labels for the four modules](ablation/rq2_manual_labels.tsv) supply the 11 known bugs and nine rejected reports used as denominators. Matches use the property ID when available and otherwise the function and property class.

The `full` rows in the per-module scores record the four-module timing profile. The module wall times sum to 9,411 s across 47 functions.
