# RQ2: VibeOS verification and component ablation

RQ2 is split into four evidence sets:

| Directory | Study |
| --- | --- |
| [`main/`](main/) | BMC-Agent and FM-Agent candidate and confirmed-finding results |
| [`bmcagent-f2f/`](bmcagent-f2f/) | Frozen BMC-Agent `f2f31711e` pipeline and its comparison runs |
| [`intermediate/`](intermediate/) | Specifications, verdicts, validation, realism, and refinement statistics |
| [`ablation/`](ablation/) | Current component ablation used for Table VII |

The main study and intermediate statistics use the June VibeOS kernel sweep.
The evaluated VibeOS source snapshot is
[`source-vibeos-866bfb9.tar.gz`](source-vibeos-866bfb9.tar.gz).
The Table VII Full row is the original RQ2 run. The three ablated
configurations were rerun through OpenRouter with the upstream provider pinned
to Anthropic; their provenance is recorded in [`ablation/`](ablation/).
