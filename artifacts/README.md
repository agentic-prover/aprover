# Paper artifacts

The [BMC-Agent implementation](../bmc_agent/) coordinates specification generation, verification, counterexample validation, and refinement. Start with [`AMCPipeline`](../bmc_agent/pipeline.py) or the [command-line entry point](../bmc_agent/cli.py) for `bmc-agent verify` and `bmc-agent verify-dir`.

| Study | Data |
| --- | --- |
| RQ1. Traditional verifiers | [AWS and LDV inputs, configurations, and results](rq1/README.md) |
| RQ2. Agentic verification | [VibeOS comparison, component ablation, and timing](rq2/vibeos/README.md) |
| RQ3. Specification quality | [ACSL and JML results and proof inputs](rq3/README.md) |
| RQ4. Real-world findings | [Counts by project, Linux mainline fixes, and published jq advisories](rq4/README.md) |

The [reproduction guide](../experiments/paper_reproduction/README.md) gives tool versions, benchmark sources, and commands. Verify the data files with `sha256sum -c artifacts/SHA256SUMS` from the repository root.
