# Agentic Model Checking: code and experiment artifacts

This repository is the public artifact for the ICSE 2027 paper
*Agentic Model Checking*. It contains the BMC-Agent implementation, the
experiment inputs and configurations, the recorded results, and scripts for
reproducing the reported measurements.

| Component | Contents |
| --- | --- |
| [`bmcagent/`](bmcagent/) | BMC-Agent source, command-line interface, tests, and reproduction scripts |
| [`rq1/`](rq1/) | RQ1 verification benchmarks and baseline results |
| [`rq2/`](rq2/) | RQ2 VibeOS study, the frozen `f2f` pipeline, intermediate statistics, and component ablation |
| [`rq3/`](rq3/) | RQ3 ACSL and JML specification-quality experiments |
| [`rq4/`](rq4/) | RQ4 real-world findings and upstream evidence |

The [artifact index](artifact-index.tsv) maps each paper study to its code
revision, configuration, result table, and raw-data archive. Check file
integrity with:

Semicolon-separated revisions identify separate runs of the same study;
`multiple` marks evidence collected across several source revisions.

~~~sh
sha256sum -c SHA256SUMS
~~~

## Quick start

Use Python 3.12, [uv](https://docs.astral.sh/uv/), and CBMC 5.95.1:

~~~sh
cd bmcagent
uv sync --frozen
uv run bmc-agent verify --source examples/simple_driver.c \
  --driver simple_driver --output artifacts/quickstart
~~~

See the [BMC-Agent guide](bmcagent/README.md) and the
[reproduction guide](bmcagent/experiments/paper_reproduction/README.md).

## Disclosure scope

The public RQ4 data include aggregate source-audited counts, published
advisories, public upstream discussion, and merged Linux commits.
