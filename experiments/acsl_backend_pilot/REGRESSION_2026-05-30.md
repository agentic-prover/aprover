# ACSL Pilot Regression Check — 2026-05-30

Question: did adding the optional ACSL/Frama-C pilot path perturb existing
BMC-Agent bug-finding behavior?

Short answer: no regression was observed in the targeted checks below. The
change is isolated to a new `acsl-pilot` command plus `bmc_agent/acsl.py`; the
existing CBMC/Kani verification modules were not modified.

## Scope

Worktree under test:

- `/mnt/disk7/jw_bmc/aprover_acsl_main`
- branch: `exp/acsl-backend-pilot`
- base: `origin/main` at `350400b`

Main control worktree:

- `/mnt/disk7/jw_bmc/aprover_main_regression`
- detached at `origin/main` / `350400b`

Embargoed findings repo cloned for case selection:

- `/mnt/disk7/jw_bmc/aprover-findings-embargoed`
- `main` at `e2fc98f`

## Checks Run

### 1. Focused ACSL tests

Command:

```bash
uv run pytest tests/test_acsl.py tests/test_dsl_paren_balanced.py -q
```

Result:

- `19 passed`

### 2. Full test suite comparison

Command on ACSL branch:

```bash
uv run pytest tests -q
```

Result:

- `1524 passed, 16 skipped, 3 failed`
- The 3 failures are all in `tests/test_phase3.py` and are unrelated to
  ACSL code paths.

Control command on clean `origin/main`:

```bash
uv run pytest \
  tests/test_phase3.py::test_upward_propagation \
  tests/test_phase3.py::test_entry_function_real_bug \
  tests/test_phase3.py::test_vacuous_spec_postcondition_violation_filtered \
  -q
```

Result:

- Same 3 tests fail on `origin/main`.
- Interpretation: these are pre-existing Phase 3 test failures, not introduced
  by the ACSL pilot.

### 3. Baseline count parity against `origin/main`

Commands:

```bash
source /mnt/disk7/jw_bmc/env.sh
uv run bmc-agent baseline --source examples/simple_driver.c --driver acsl_regression_simple_driver --output artifacts/acsl_regression
uv run bmc-agent baseline --source examples/sensor_hub.c --driver acsl_regression_sensor_hub --output artifacts/acsl_regression
uv run bmc-agent baseline --source examples/block_device.c --driver acsl_regression_block_device --output artifacts/acsl_regression
```

The same commands were run in the clean `origin/main` control worktree.

| Source | ACSL branch findings | origin/main findings | Parity |
|---|---:|---:|---|
| `examples/simple_driver.c` | 344 | 344 | yes |
| `examples/sensor_hub.c` | 177 | 177 | yes |
| `examples/block_device.c` | 264 | 264 | yes |

Representative expected signals still present:

- `sensor_hub.c`: `latest_value.division-by-zero.1`,
  `record_reading.assertion.2`
- `simple_driver.c`: `rb_write.division-by-zero.1`
- `block_device.c`: `blk_seek.*` findings still emitted in the baseline list

### 4. Existing finding harness replays

The public repo contains self-contained libarchive finding harnesses under
`findings/v5/`. These are useful for low-cost regression because they bypass
LLM nondeterminism and directly test the CBMC surface used by earlier
BMC-Agent findings.

Commands:

```bash
source /mnt/disk7/jw_bmc/env.sh
timeout 60 cbmc findings/v5/archive_acl_text_len.harness.c --unwind 4 --bounds-check --pointer-check --signed-overflow-check --unsigned-overflow-check
timeout 60 cbmc findings/v5/next_field.harness.c --unwind 4 --bounds-check --pointer-check --signed-overflow-check --unsigned-overflow-check
timeout 60 cbmc findings/v5/archive_acl_clear.harness.c --unwind 4 --bounds-check --pointer-check --signed-overflow-check --unsigned-overflow-check
```

Results:

| Harness | Result | Key signal |
|---|---|---|
| `archive_acl_text_len.harness.c` | `VERIFICATION FAILED` | overflow/no-body/unwind failures still surface |
| `next_field.harness.c` | `VERIFICATION FAILED` | `next_field.pointer_dereference.317` still fails |
| `archive_acl_clear.harness.c` | `VERIFICATION FAILED` | known artifact-class free/pointer failures still surface |

Interpretation: old CBMC finding signals are still reproducible after the ACSL
pilot change. This does not re-audit whether each finding is a real bug; it
checks that the original BMC signal path was not broken.

## Embargoed Findings Used for Case Selection

The private findings repo was cloned and inspected. It currently records:

- libarchive canonical confirmed/upstream-fixed/false-positive records.
- an AWS Neuron unconfirmed source-audit case requiring KASAN validation.
- recent OSS-Fuzz candidate audits, many explicitly marked FP or needing
  manual reproduction.

For this regression check I did not run long upstream builds or disclosure
reproducers. The decision question was narrower: whether adding ACSL perturbs
existing BMC-Agent behavior. The low-cost evidence above is sufficient for
that question. A separate reproducer/KASAN/ASAN campaign should be tracked as
finding validation, not ACSL backend regression.

## Conclusion

The ACSL pilot appears low-risk to merge as an optional path:

- It does not alter the existing `verify`, `baseline`, CBMC backend, harness
  generator, dynamic validator, or triage modules.
- Baseline outputs match `origin/main` on three representative in-repo sources.
- Existing libarchive finding harnesses still reproduce CBMC failures.
- Full-suite failures are reproduced on clean `origin/main`, so they are not
  ACSL regressions.
