# RQ2 intermediate statistics

This directory recomputes the intermediate VibeOS statistics reported in RQ2
from the surviving BMC-Agent run logs.

## Scope

There are 52 VibeOS logs. For each module, the canonical run is the last run
that reached `AMC Pipeline END`; this yields 25 runs, 608 functions with
generated specifications, and 600 functions with BMC verdicts.

The main counts are:

- 546/600 harnesses compiled before repair and 599/600 after repair;
- 3,670 raw counterexample records deduplicated to 785 groups;
- 161 distinct `(function, property)` pairs reaching the realism audit;
- 48 realistic and 152 unrealistic audit events;
- 611 refinement iterations, of which 509 end without a proposal;
- 95 guard rejections and 85 logged guard checks;
- 21 source-confirmed findings among 43 final-report candidates.

Token usage is recorded for 1,107 of 2,586 agent calls. At the listed model
prices, those recorded calls imply US$38.40. Scaling by call count gives an
estimated US$89.71 for all calls. The logs do not associate each usage record
with an individual function, so no recorded-call function count or direct
per-function cost is inferred.

## Files

- `results/RESULTS.txt`: human-readable recomputation.
- `results/sweep_totals.json`: machine-readable totals and price basis.
- `results/sweep_per_module.csv`: per-module breakdown.
- `results/inventory.json` and `results/runs.txt`: log inventory and canonical
  run selection.
- `runners/`: the standard-library scripts used to recompute every figure.
- `raw/sweep_run_logs.tar.gz`: all 52 source logs; run selection occurs during
  analysis.

## Reproduction

```sh
mkdir -p /tmp/vibeos-logs
tar -xzf raw/sweep_run_logs.tar.gz -C /tmp/vibeos-logs
AMC_FINDINGS=/tmp/vibeos-logs \
BQ6_OUT="$PWD/results" \
python3 runners/sweep_stats.py > results/RESULTS.txt
```

Use `python3 runners/sweep_stats.py --show-runs` to inspect the 52 runs and
the 25 canonical selections.
