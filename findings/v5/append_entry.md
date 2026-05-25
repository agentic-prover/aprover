# v5 confirmed finding: `append_entry`

**Status**: realism-confirmed (`realism.verdict == realistic AND confidence != unlikely`).
**Generated**: 2026-05-25T05:51:53.971986Z
**Source**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry/bug_reports/strcpy.pointer_dereference.11.json`

## Target
- **File**: `libarchive/archive_acl.c`
- **Function**: `append_entry`
- **Violated property**: `strcpy.pointer_dereference.11`
- **Call chain**: `archive_acl_to_text_l -> append_entry`

## bmc-agent-sec verdict
| Layer | Result |
|---|---|
| CBMC | counterexample found |
| Realism (Sonnet 4.5, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs in strcpy at line 3891 in append_entry when copying 'owner@' string. Looking at the call chain: archive_acl_to_text_l (line 3783-3866) → append_entry (line 3877-3987). The counterexample shows prefix='\x80\x10\x00\x08' (a 4-byte buffer), tag=10005 (ARCHIVE_ENTRY_ACL_MASK), type=256, perm=42. At line 3882-3884, if prefix is non-NULL, strcpy(*p, prefix) is called and *p is advanced by strlen(*p). The prefix buffer in the witness is only 4 bytes with _prefix_len=4, but contains non-null-terminated data (ends with value 8, not 0). When strlen is called on this at line 3884, it will read beyond the 4-byte buffer looking for a null terminator, then strcpy will write that many+1 bytes (including the final null) into *p. The _p_backing buffer is only 5 bytes, so after writing prefix, there's minimal space left. At line 3886-3925, the switch statement processes tag=10005 (ARCHIVE_ENTRY_ACL_MASK), which at line 3907 does strcpy(*p, 'mask'), requiring 5 bytes including null. Combined with earlier writes, this exceeds the 5-byte _p_backing buffer. An attacker controlling ACL text input via archive_acl_from_text_l can supply a malformed prefix or construct ACL entries that cause the buffer to be undersized relative to the formatted output, triggering the OOB write in strcpy.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file with crafted POSIX.1e ACL entries. By controlling the ACL text format parsed by archive_acl_from_text_l (line 4370-4587), they can inject entries with a mask tag and specific prefix patterns. When archive_acl_to_text_l is called to serialize these ACLs back to text (common during archive extraction or listing), the function allocates a buffer based on archive_acl_text_len but the calculation can be incorrect if prefix contains non-printable characters or the name field has unexpected encoding. The strcpy at line 3908 (or 3891) writes 'mask' (or 'owner@') into the undersized buffer *p, overwriting adjacent heap metadata or other sensitive structures, leading to memory corruption exploitable for code execution or information disclosure.

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record:

- (none)

## Reproduction

- **CBMC harness**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry/bug_reports/harness.c`
- **Spec used**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry/bug_reports/spec.json` (lite-mode, pre=post=true)
- **CBMC raw output**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry/bug_reports/cbmc_result.json`
- **Classifier state**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/append_entry/bug_reports/classification.json`

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
