# v5 confirmed finding: `archive_acl_to_text_w`

**Status**: realism-confirmed (`realism.verdict == realistic AND confidence != unlikely`).
**Generated**: 2026-05-25T05:51:53.972487Z
**Source**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_w/bug_reports/archive_acl_to_text_w.pointer_dereference.77.json`

## Target
- **File**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_to_text_w`
- **Violated property**: `archive_acl_to_text_w.pointer_dereference.77`
- **Call chain**: `archive_acl_to_text_w`

## bmc-agent-sec verdict
| Layer | Result |
|---|---|
| CBMC | counterexample found |
| Realism (Sonnet 4.5, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 3655 where `*wp++ = L'\0';` writes beyond the allocated buffer. Looking at the buffer allocation at line 3605: `wp = ws = malloc(length * sizeof(*wp));` where `length` is computed by `archive_acl_text_len()` at line 3598. The key issue is that `archive_acl_text_len()` counts the number of wchar_t characters needed, but the actual writing loop can write MORE characters than counted due to several factors:

1. Line 3616 writes a separator after each POSIX ACL entry (user/group/other base permissions)
2. Lines 3641-3649 iterate through `acl->acl_head` entries, writing a separator before each (line 3641: `if (count > 0) *wp++ = separator;`)
3. Line 3655 writes the null terminator

The counting in `archive_acl_text_len()` (lines 3473-3579) attempts to account for separators at line 3568 (`length ++;`), but the logic is complex and error-prone. The counterexample shows `length = 2ul` but the code writes at least 3 wchar_t values:
- Line 3616: separator after first base permission
- Line 3621: separator after second base permission  
- Line 3655: null terminator

This is a classic off-by-one buffer overflow. The `length` calculation doesn't properly account for all the separators and the null terminator that get written. An attacker can craft ACL entries (via `archive_acl_add_entry_w_len()` or by parsing ACL text) that cause the length calculation to undercount, leading to heap buffer overflow when `archive_acl_to_text_w()` is called.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file (tar, zip, etc.) containing crafted ACL metadata. When libarchive parses this archive and calls `archive_acl_to_text_w()` to convert the ACL to text format (e.g., for display or validation), the function allocates a buffer that is too small based on the flawed length calculation. The subsequent writes overflow the heap buffer, potentially corrupting adjacent heap structures. This could lead to arbitrary code execution through heap metadata corruption or information disclosure by overwriting sensitive data.

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record:

- (none)

## Reproduction

- **CBMC harness**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_w/bug_reports/harness.c`
- **Spec used**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_w/bug_reports/spec.json` (lite-mode, pre=post=true)
- **CBMC raw output**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_w/bug_reports/cbmc_result.json`
- **Classifier state**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_w/bug_reports/classification.json`

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
