# v5 confirmed finding: `append_entry_w`

**Status**: realism-confirmed (`realism.verdict == realistic AND confidence != unlikely`).
**Generated**: 2026-05-25T05:51:53.972098Z
**Source**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry_w/bug_reports/append_entry_w.pointer_dereference.71.json`

## Target
- **File**: `libarchive/archive_acl.c`
- **Function**: `append_entry_w`
- **Violated property**: `append_entry_w.pointer_dereference.71`
- **Call chain**: `archive_acl_to_text_w -> append_entry_w`

## bmc-agent-sec verdict
| Layer | Result |
|---|---|
| CBMC | counterexample found |
| Realism (Sonnet 4.5, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 3721 in append_entry_w where *(*wp)++ dereferences wp after potentially writing beyond the allocated buffer. The function archive_acl_to_text_w (lines 3580-3662) calculates the required buffer length via archive_acl_text_len (lines 3473-3579), then allocates 'length * sizeof(*wp)' bytes (line 3605). However, the length calculation has a critical flaw: at line 3551 it adds 27 characters for NFSv4 ACL entries, but the actual append_entry_w can write more. Specifically, lines 3745-3757 write up to 14 permission characters (nfsv4_acl_perm_map_size=14), then 7 flag characters (nfsv4_acl_flag_map_size=7), plus colons and type strings. When flags & 0x00000010 is zero, each character position is filled, leading to more characters than the 27 budgeted. An attacker controlling ACL data (via archive_entry_acl_add_entry_w or similar APIs) with NFSv4 ACL types can trigger this. The counterexample shows type=0x900 (DENY|ALLOW bits mixed, though unusual, type validation at lines 3279-3308 allows multiple type bits), tag=268445459 (passes validation since it's checked at lines 3309-3328 but unusual values may pass through), flags=4, and perm=15. With these values, append_entry_w writes more than allocated, causing wp to advance beyond the buffer end, triggering the pointer-outside-object-bounds violation.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive containing NFSv4 ACL entries with specific combinations of permission bits (setting all 14 permission flags) and flag bits (setting all 7 inheritance flags) with flags parameter set to exclude ARCHIVE_ENTRY_ACL_STYLE_COMPACT (0x00000010). When archive_acl_to_text_w is called (e.g., via archive_entry_acl_to_text_w from user code processing the archive), the length calculation underestimates the required buffer size. As append_entry_w writes the full permission and flag strings (potentially 14+7=21 characters plus separators and type string, exceeding the budgeted 27), the write pointer advances beyond the allocated buffer, causing a buffer overflow that could lead to memory corruption or information disclosure.

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record:

- (none)

## Reproduction

- **CBMC harness**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry_w/bug_reports/harness.c`
- **Spec used**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry_w/bug_reports/spec.json` (lite-mode, pre=post=true)
- **CBMC raw output**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry_w/bug_reports/cbmc_result.json`
- **Classifier state**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry_w/bug_reports/classification.json`

To re-run the full sweep:

```
. /tmp/.bmc_key && .venv/bin/python -m bmc_agent.cli verify-dir \
  --source-dir /tmp/libarchive_auto_corpus \
  --driver v5_rerun \
  --output /tmp/libarchive_v5_rerun \
  --include-dir /tmp/libarchive_bench/libarchive/build \
  --include-dir /tmp/libarchive_bench/libarchive/libarchive \
  --lite-mode \
  --enable-realism-check --enable-realism-thinking \
  --enable-dynamic-validation \
  --enable-feedback-loop --feedback-max-iters 10 \
  --enable-flag-selection \
  --follow-adjacent-rounds 2 \
  --exclude 'test_*' -D HAVE_CONFIG_H
```

## Honest caveats (read before upstream reporting)

- Dynamic outcome was **no_record**. WEAK evidence: dynamic harness did not reproduce the crash; realism LLM verdict is the only evidence.
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling state"). **Independent code-level verification of that condition is needed before reporting upstream.**
- realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts — the latest one wins in this summary.
