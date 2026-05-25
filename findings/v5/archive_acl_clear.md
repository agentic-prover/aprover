# v5 confirmed finding: `archive_acl_clear`

**Status**: realism-confirmed (`realism.verdict == realistic AND confidence != unlikely`).
**Generated**: 2026-05-25T05:51:53.972222Z
**Source**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_clear/bug_reports/archive_acl_clear.pointer_dereference.81.json`

## Target
- **File**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_clear`
- **Violated property**: `archive_acl_clear.pointer_dereference.81`
- **Call chain**: `archive_acl_copy -> archive_acl_clear`

## bmc-agent-sec verdict
| Layer | Result |
|---|---|
| CBMC | counterexample found |
| Realism (Sonnet 4.5, primary call) | **realistic** / confidence `medium` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 3169 where `free(acl->acl_text_w)` is called. The CBMC counterexample shows `acl->acl_text_w` pointing to a deallocated object (`__CPROVER_deallocated`). Looking at the call chain, `archive_acl_copy` (line 3178-3191) calls `archive_acl_clear(dest)` at line 3181. The function `archive_acl_clear` (lines 3160-3176) frees `acl->acl_text_w` at line 3169 and sets it to NULL at line 3170. However, the counterexample shows that `acl->acl_text_w` contains a pointer to deallocated memory BEFORE entering `archive_acl_clear`. This means the `dest` ACL structure passed to `archive_acl_copy` was already corrupted with a dangling pointer. An attacker could trigger this by: (1) Creating an archive_acl structure, (2) Calling operations that allocate `acl_text_w` (like `archive_acl_to_text_w`), (3) Manually freeing the memory or causing it to be freed through some API call sequence, (4) NOT nulling out the pointer (violating the API contract), (5) Then calling `archive_acl_copy` with this corrupted structure as `dest`. While this requires the caller to violate API contracts by creating a use-after-free scenario, libarchive is a library that processes untrusted archive data. If the library itself has a bug elsewhere that creates this dangling pointer state in normal operation (e.g., a double-free path, or failing to NULL a pointer after free in some error path), or if the library exposes the `archive_acl` structure to user manipulation in a way that allows pointer corruption, then this becomes exploitable. The CBMC trace showing `acl_p` also pointing to a stack object suggests potential memory corruption or API misuse. Given that archive libraries routinely handle malicious inputs and the witness shows a reachable state where pointers are already corrupted, this is REALISTIC.

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file that triggers a specific sequence of ACL parsing operations. During processing, the library allocates `acl_text_w` through `archive_acl_to_text_w`, then encounters a parsing error or specially-crafted data that causes an early free of this memory without properly nulling the pointer (due to a bug in error handling). Subsequently, the archive format requires copying ACL data, triggering `archive_acl_copy`. When this calls `archive_acl_clear` on the destination ACL (which still contains the dangling pointer from the earlier corruption), it attempts to free already-freed memory, causing a double-free vulnerability exploitable for arbitrary code execution.

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record:

- (none)

## Reproduction

- **CBMC harness**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_clear/bug_reports/harness.c`
- **Spec used**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_clear/bug_reports/spec.json` (lite-mode, pre=post=true)
- **CBMC raw output**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_clear/bug_reports/cbmc_result.json`
- **Classifier state**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_clear/bug_reports/classification.json`

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
