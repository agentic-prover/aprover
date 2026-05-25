# v5 confirmed finding: `archive_acl_text_len`

**Status**: realism-confirmed (`realism.verdict == realistic AND confidence != unlikely`).
**Generated**: 2026-05-25T05:51:53.972319Z
**Source**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/bug_reports/archive_acl_text_len.overflow.12.json`

## Target
- **File**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_text_len`
- **Violated property**: `archive_acl_text_len.overflow.12`
- **Call chain**: `archive_acl_to_text_l -> archive_acl_text_len`

## bmc-agent-sec verdict
| Layer | Result |
|---|---|
| CBMC | counterexample found |
| Realism (Sonnet 4.5, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The overflow occurs at line 3568 in archive_acl_text_len when computing 'length + (unsigned long int)1' (the final 'length ++' statement). The function iterates over ACL entries and accumulates various length increments without any overflow checking. An attacker can control the ACL structure by calling archive_acl_add_entry or archive_acl_from_text_l to populate acl->acl_head with many entries. Each iteration adds multiple increments to 'length' (lines 3495-3568), including fixed constants (lines 3496-3518), name lengths from archive_mstring_get_mbs_l (lines 3532-3539), and additional calculations (lines 3551-3566). The CBMC witness shows length = SIZE_MAX-1 (18446744073709551615ul) before the final increment, which would overflow. This is achievable by: (1) creating a very long linked list of ACL entries via repeated archive_acl_add_entry calls, or (2) providing ACL entries with extremely long name strings via archive_mstring_copy_mbs_len, or (3) a combination causing the cumulative length to approach SIZE_MAX. The call chain archive_acl_to_text_l → archive_acl_text_len (line 3801) shows this is reachable from public API. The counterexample shows len=18446744073709551612ul from archive_mstring_get_mbs_l (line 3539), meaning a maliciously crafted mstring could contribute massively to the overflow. No overflow guards exist in the loop (lines 3484-3569).

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file (e.g., tar, cpio, zip) containing ACL entries with extremely long username/group name strings or an enormous number of ACL entries. When libarchive parses this file and calls archive_acl_to_text_l (for example, to display ACL text or convert to a specific format), the archive_acl_text_len function accumulates lengths without checking for overflow. By carefully sizing the input—either through thousands of ACL entries or through name strings approaching SIZE_MAX in aggregate—the attacker causes 'length' to reach SIZE_MAX-1. The final 'length++' at line 3568 then wraps to 0, causing malloc(0) at line 3808, resulting in a tiny or NULL allocation. Subsequent string operations write to this under-allocated buffer, leading to heap corruption and potential code execution.

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record:

- (none)

## Reproduction

- **CBMC harness**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/bug_reports/harness.c`
- **Spec used**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/bug_reports/spec.json` (lite-mode, pre=post=true)
- **CBMC raw output**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/bug_reports/cbmc_result.json`
- **Classifier state**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/bug_reports/classification.json`

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
