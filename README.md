# Agentic Model Checking

This artifact accompanies the ICSE 2027 paper *Agentic Model Checking*. It contains BMC-Agent, the evaluation data, and instructions for running the tool and using the experiment materials.

| Resource | Location |
| --- | --- |
| BMC-Agent implementation | [`bmc_agent/`](bmc_agent/) |
| Experiment data for RQ1–RQ4 | [Artifact index](artifacts/README.md) |
| Setup, experiment commands, and proof replay | [Reproduction guide](experiments/paper_reproduction/README.md) |

## Quick start

Use Python 3.12, [uv](https://docs.astral.sh/uv/), and CBMC 5.95.1 on `PATH`. With `ANTHROPIC_API_KEY` set, run:

~~~sh
uv sync --frozen
uv run bmc-agent verify --source examples/simple_driver.c \
  --driver simple_driver --output artifacts/quickstart
~~~

The output directory contains the generated specifications, verification results, and any reported findings. See [`LICENSE`](LICENSE) for the software license.
