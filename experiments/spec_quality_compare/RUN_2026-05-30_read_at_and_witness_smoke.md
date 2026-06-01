# Read-At VDR and Witness-Preservation Smoke

Date: 2026-05-30

Questions:

1. Does the SpecSyn-style mutation metric distinguish a strong pointer/bounds
   spec from a weak verifier-passing spec?
2. Can we mechanically flag the `ncdev_bar_read` overconstraint failure mode?

## Read-At Strong Spec

Command:

```bash
uv run python experiments/spec_quality_compare/acsl_mutation_smoke.py \
  --source experiments/spec_quality_compare/read_at.c \
  --spec-json experiments/spec_quality_compare/read_at_strong_spec.json \
  --function read_at \
  --mutation-set read_at \
  --output artifacts/spec_quality_compare/read_at_strong_mutation_smoke \
  --add-assigns-nothing \
  --wp-timeout 10 \
  --timeout 120 \
  --cpus 2
```

Artifact:

- `artifacts/spec_quality_compare/read_at_strong_mutation_smoke/report.json`

Result:

| case | Frama-C/WP status | proved goals | killed |
|---|---|---:|---:|
| original | success | 4/4 | no |
| return_first_element | unproved | 3/4 | yes |
| return_zero | unproved | 3/4 | yes |
| return_last_element | unproved | 3/4 | yes |

Mutation score: 3/3 = 1.0.

## Read-At Weak Spec

Command:

```bash
uv run python experiments/spec_quality_compare/acsl_mutation_smoke.py \
  --source experiments/spec_quality_compare/read_at.c \
  --spec-json experiments/spec_quality_compare/read_at_weak_spec.json \
  --function read_at \
  --mutation-set read_at \
  --output artifacts/spec_quality_compare/read_at_weak_mutation_smoke \
  --add-assigns-nothing \
  --wp-timeout 10 \
  --timeout 120 \
  --cpus 2
```

Artifact:

- `artifacts/spec_quality_compare/read_at_weak_mutation_smoke/report.json`

Result:

| case | Frama-C/WP status | proved goals | killed |
|---|---|---:|---:|
| original | success | 3/3 | no |
| return_first_element | success | 3/3 | no |
| return_zero | success | 3/3 | no |
| return_last_element | success | 3/3 | no |

Mutation score: 0/3 = 0.0.

Interpretation:

Both specs verify on the original program. The mutation metric separates them:
the strong spec rejects all tested behavioral variants, while the weak spec
accepts every variant. This is exactly the SpecSyn lesson: verifier pass is
necessary but not sufficient.

## Ncdev-Bar-Read Witness Preservation

Command:

```bash
uv run python experiments/spec_quality_compare/witness_preservation_smoke.py
```

Artifact:

- `artifacts/spec_quality_compare/ncdev_bar_read_witness_smoke/report.json`

Encoded witness:

- `bar = 2`
- `data_count = 8`
- `address_count = 1`
- `reg_addresses_capacity = 1`

Result:

| precondition case | accepts witness | overconstraint for bug discovery |
|---|---:|---:|
| `true` | yes | no |
| `valid_range(reg_addresses, 0, address_count)` | yes | no |
| `valid_range(reg_addresses, 0, data_count)` | no | yes |
| `bar == 0 || data_count <= 1` | no | yes |

Interpretation:

The known failure mode is mechanically detectable: a spec that assumes
`valid_range(reg_addresses, 0, data_count)` excludes the violating caller state
where the caller allocated one element but passed `data_count > 1`. That spec
may make the callee verify clean, but it is not witness-preserving for bug
discovery.

Decision:

The spec-quality comparison should report at least three columns:

- original validity
- mutation/VDR score
- witness-preservation or overconstraint status for known-bug cases

This is enough signal to avoid broad benchmark expansion until we have one
real generated BMC-Agent spec and one ACSL baseline spec for the same cases.
