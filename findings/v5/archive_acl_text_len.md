# bmc-agent-sec confirmed finding: `archive_acl_text_len`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T05:36:33.742943+00:00
**Strongest record**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/bug_reports/archive_acl_text_len.overflow.12.json`

## Target

- **File**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_text_len`
- **Violated property**: `archive_acl_text_len.overflow.12`
- **Call chain**: `archive_acl_to_text_l -> archive_acl_text_len`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found |
| Realism (LLM, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The overflow occurs at line 3568 in archive_acl_text_len when computing 'length + (unsigned long int)1' (the final 'length ++' statement). The function iterates over ACL entries and accumulates various length increments without any overflow checking. An attacker can control the ACL structure by calling archive_acl_add_entry or archive_acl_from_text_l to populate acl->acl_head with many entries. Each iteration adds multiple increments to 'length' (lines 3495-3568), including fixed constants (lines 3496-3518), name lengths from archive_mstring_get_mbs_l (lines 3532-3539), and additional calculations (lines 3551-3566). The CBMC witness shows length = SIZE_MAX-1 (18446744073709551615ul) before the final increment, which would overflow. This is achievable by: (1) creating a very long linked list of ACL entries via repeated archive_acl_add_entry calls, or (2) providing ACL entries with extremely long name strings via archive_mstring_copy_mbs_len, or (3) a combination causing the cumulative length to approach SIZE_MAX. The call chain archive_acl_to_text_l → archive_acl_text_len (line 3801) shows this is reachable from public API. The counterexample shows len=18446744073709551612ul from archive_mstring_get_mbs_l (line 3539), meaning a maliciously crafted mstring could contribute massively to the overflow. No overflow guards exist in the loop (lines 3484-3569).

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file (e.g., tar, cpio, zip) containing ACL entries with extremely long username/group name strings or an enormous number of ACL entries. When libarchive parses this file and calls archive_acl_to_text_l (for example, to display ACL text or convert to a specific format), the archive_acl_text_len function accumulates lengths without checking for overflow. By carefully sizing the input—either through thousands of ACL entries or through name strings approaching SIZE_MAX in aggregate—the attacker causes 'length' to reach SIZE_MAX-1. The final 'length++' at line 3568 then wraps to 0, causing malloc(0) at line 3808, resulting in a tiny or NULL allocation. Subsequent string operations write to this under-allocated buffer, leading to heap corruption and potential code execution.

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/`:

- `bug_reports/archive_acl_text_len.overflow.10.json`
- `bug_reports/archive_acl_text_len.overflow.12.json`
- `bug_reports/archive_acl_text_len.overflow.8.json`
- `bug_reports/unnamed_1779685657494.json`

## Reproduction

- **CBMC harness**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/harness.c`
- **Spec used**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/spec.json`
- **CBMC raw output**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/cbmc_result.json`
- **Classifier state**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_text_len/classification.json`





## Honest caveats (read before upstream reporting)

- **Dynamic outcome was `no_record`.** WEAK evidence: the dynamic harness did NOT reproduce the crash with the concrete CBMC witness. The realism LLM's vote is the only evidence.
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling pointer state"). **Independent code-level verification of that condition is required before reporting upstream.**
- Realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts; this report uses the strongest realistic record by mtime.
