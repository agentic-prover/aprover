# RQ1 benchmark manifest

The input archives contain the preprocessed C files and original task definitions for the `unreach-call` tasks from [SV-Benchmarks at `2e1723f`](https://github.com/sosy-lab/sv-benchmarks/tree/2e1723fde6aa65a250dcb677efa45edaa4b6b631).

| Suite | Input archive | Tasks | Benchmark data model | Evaluation data model |
| --- | --- | ---: | --- | --- |
| `aws-c-common` | [AWS tasks](sources/aws-c-common-tasks.tar.gz) | 27 | LP64 | LP64 |
| `ldv-linux-3.4-simple` | [LDV tasks](sources/ldv-linux-3.4-simple-unsafe-tasks.tar.gz) | 29 | LP64 | ILP32 |

The [input lock](../../experiments/paper_reproduction/benchmark_inputs.json) stores a SHA-256 for every input. BMC-Agent uses Claude Sonnet 4.5. The verifier versions are CBMC 5.95.1, ESBMC 8.3.0, and CPAchecker 4.2.2. See the [results and configuration notes](README.md) for the per-suite settings.
