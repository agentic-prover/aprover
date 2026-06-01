# ACSL Backend Pilot

This directory holds the smallest decision-oriented probe for the ACSL
direction. It is not a benchmark sweep.

Question: can a BMC-Agent DSL function spec be translated into an ACSL
contract that Frama-C/WP can use on the same C source?

Current scope:

- Translate common function-level `requires` / `ensures` clauses.
- Optionally recover plain C `assert(EXPR);` into ACSL `//@ assert EXPR;`.
- Run Frama-C/WP through Docker by default.
- Record unsupported DSL clauses instead of silently pretending they have ACSL
  semantics.

Known non-goals for this first probe:

- No automatic loop invariant synthesis.
- No full ACSL frame-condition inference. `--add-assigns-nothing` is opt-in
  and only sound for pure functions.
- No attempt to turn BMC counterexample validation into a deductive proof task.

Smoke command:

```bash
uv run bmc-agent acsl-pilot \
  --source experiments/acsl_backend_pilot/max2.c \
  --driver acsl_max2_smoke \
  --output artifacts/acsl_backend_pilot \
  --spec-json experiments/acsl_backend_pilot/max2_spec.json \
  --function max2 \
  --recover-asserts \
  --add-assigns-nothing \
  --wp-timeout 10 \
  --timeout 120
```
