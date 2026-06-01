# Max2 ACSL Mutation Smoke

Date: 2026-05-30

Question: can the SpecSyn-style mutation/variant-discrimination metric run on
the current BMC-Agent DSL-to-ACSL pilot?

Command:

```bash
uv run python experiments/spec_quality_compare/acsl_mutation_smoke.py \
  --add-assigns-nothing \
  --wp-timeout 10 \
  --timeout 120 \
  --cpus 2
```

Result artifact:

- `artifacts/spec_quality_compare/max2_mutation_smoke/report.json`

Summary:

| case | equivalent hint | Frama-C/WP status | proved goals | killed |
|---|---:|---|---:|---:|
| original | no | success | 13/13 | no |
| return_left_argument | no | unproved | 10/12 | yes |
| return_zero | no | unproved | 8/12 | yes |
| return_minimum | no | unproved | 10/13 | yes |
| tie_break_equal_case | yes | success | 13/13 | no |

Mutation score:

- non-equivalent mutants tried: 3
- killed: 3
- score: 1.0

Interpretation:

The translated ACSL contract is strong enough to reject simple behavioral
mutations of `max2` while proving the original program. The equal-case tie
break mutant is a useful reminder that VDR must filter or mark semantically
equivalent mutants; otherwise a good spec is unfairly penalized.

Decision:

Proceed to one small bounds/pointer case before attempting any broad benchmark
comparison. Do not expand the mutation set yet; the next question is whether
the same metric catches weak or overconstraining specs on a memory-safety case.
