# VibeOS QEMU Dynamic Validation Admission Protocol

This protocol defines when a VibeOS QEMU replay may be added to BMC-Agent dynamic validation. The goal is to add target-side evidence without fitting replay code to known benchmark outcomes.

## Scope

This protocol applies to `scripts/vibeos_qemu_dynamic_replay.py`.

The QEMU adapter is not a replacement for the normal BMC-Agent pipeline. It is an optional target replay backend for bare-metal cases where the host GCC harness is a poor runtime model.

## Outcome Classes

`confirmed`
: The target replay observes a target-visible fault or a semantic mismatch. This is QEMU-confirmed evidence.

`observed_safety_concern`
: The target replay reaches an unsafe operation, such as an unchecked public pointer or invalid opaque handle, but VibeOS does not expose a crash or panic marker. This is counted as a safety concern, not a QEMU-confirmed crash.

`inconclusive`
: The replay is unsupported, fails to build or boot, does not reach the target operation, times out before a marker, or emits no recognizable marker.

`not_triggered`
: Reserved for replay rules where clean target execution is a meaningful negative signal. Do not use this for VibeOS null-pointer or invalid-pointer classes unless the target has a defined exception model that makes no-fault execution meaningful.

## Admission Rule

A new replay case may be added only if all of the following are true:

1. It represents a reusable bug or API class, not a single benchmark result row.
2. The class can be described before looking at the current run's final verdict.
3. The replay target is a public or subsystem-level boundary that a caller can plausibly reach.
4. The injected code is small, deterministic, and hand-auditable.
5. The expected marker is defined by target behavior, not by matching a previous report's desired outcome.
6. The replay records `category`, `selection_rule`, and `target_event` in the catalog metadata.
7. The replay does not execute LLM-generated shell commands or arbitrary host commands.

## Non-Admission Cases

Do not add a replay case when:

1. The only reason is that a previous `vibeos_full_check10` row exists.
2. The injected code reconstructs a benchmark witness exactly without a reusable rule.
3. The case depends on hidden test-only state or generated stubs that do not exist in VibeOS.
4. A clean QEMU run would be misread as disproving a C-language safety issue on a target without Linux-like `SIGSEGV` or `SIGABRT`.
5. The replay requires broad kernel rewrites, custom shell scripts from the LLM, or non-deterministic interaction.

## Current Admitted Classes

| Case | Category | Selection Rule | Target Event |
|---|---|---|---|
| `net_get_mac_null` | `public_api_pointer_guard` | Public API writes to caller-provided output buffer | `UNGUARDED_NULL_POINTER` |
| `kapi_file_size_invalid_ptr` | `public_api_handle_guard` | Public API accepts an opaque handle and dereferences it after only a NULL check | `UNGUARDED_INVALID_POINTER` |
| `hal_dma_fb_copy_overflow` | `dimension_arithmetic_overflow` | Framebuffer copy computes dimensions before rejecting overflowing geometry | `SEMANTIC_MISMATCH` |

## Review Checklist

Before adding a new case:

1. Write the proposed `category`, `selection_rule`, and `target_event`.
2. Explain the target-side marker and why it maps to `confirmed`, `observed_safety_concern`, or `inconclusive`.
3. Confirm the replay is not derived from a BUG ID or previous result row.
4. Add unit tests for case resolution, catalog metadata, and outcome parsing.
5. Run a build-only smoke before a QEMU run.

## Useful Commands

List the current catalog:

```bash
uv run python scripts/vibeos_qemu_dynamic_replay.py --list-cases
```

Run a build-only smoke for a selected rule:

```bash
uv run python scripts/vibeos_qemu_dynamic_replay.py \
  --repo /mnt/disk7/jw_bmc/vibeos \
  --case net_get_mac_null \
  --workdir /tmp/bmc_vibeos_rule_smoke \
  --build-only \
  --build-timeout 180
```

Use QEMU selectively from BMC-Agent:

```bash
export BMC_AGENT_DYNAMIC_VALIDATION_BACKEND=hybrid
export BMC_AGENT_DYNAMIC_QEMU_ENTRIES=hal_dma_fb_copy,net_get_mac,kapi_file_size
export BMC_AGENT_DYNAMIC_QEMU_COMMAND="uv run python scripts/vibeos_qemu_dynamic_replay.py --repo /mnt/disk7/jw_bmc/vibeos --case auto --build-timeout 180 --qemu-timeout 25"
export BMC_AGENT_DYNAMIC_QEMU_TIMEOUT=240
```

In `hybrid` mode, only entries listed in `BMC_AGENT_DYNAMIC_QEMU_ENTRIES` use QEMU. Other dynamic validation attempts keep the normal host backend.
