# bmc-agent-sec confirmed finding: `archive_acl_to_text_l`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T05:36:33.744383+00:00
**Strongest record**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_l/bug_reports/archive_acl_text_len.overflow.12.json`

## Target

- **File**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_to_text_l`
- **Violated property**: `archive_acl_text_len.overflow.12`
- **Call chain**: `archive_acl_to_text_l`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found |
| Realism (LLM, primary call) | **realistic** / confidence `high` |
| Dynamic harness (GCC + signal handlers) | **no_record**, signal=`none` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 2094 in archive_acl_text_len when computing 'length + (unsigned long int)1'. The counterexample shows length=18446744073709551615ul (SIZE_MAX), causing overflow when adding 1. Tracing the call chain: archive_acl_to_text_l (line 3801) calls archive_acl_text_len (line 3473) with attacker-controlled acl structure. The acl structure can be populated via archive_acl_add_entry (line 3193) or archive_acl_add_entry_w_len (line 3210), both of which are public APIs that accept arbitrary type, permset, tag, and name parameters. An attacker can craft an ACL with numerous entries that cause archive_acl_text_len to accumulate length until it approaches SIZE_MAX. Specifically, at line 3519 in archive_acl_text_len, each ACL entry contributes to length based on name length, tag type, and permission string representations. With a large number of entries (e.g., 10001 entries via acl_head->next chain as shown in counterexample), or entries with long names via archive_mstring functions, the accumulated length can reach SIZE_MAX. The function then adds 1 at line 3568 ('length ++') for the null terminator, causing unsigned overflow. The overflow is then used at line 3808 to malloc(length * sizeof(*p)), which with wrapped-around small value would allocate insufficient memory, leading to buffer overrun when archive_acl_to_text_l writes the ACL string starting at line 3816. The malloc contract (line 901) guarantees valid pointer OR NULL, but the subsequent strlen check at line 3861 would pass with a too-small buffer, and the earlier writes (lines 3816-3858) would have already overflowed. The CBMC witness shows this is reachable with acl_head containing one entry, but the length calculation logic allows arbitrary accumulation through repeated API calls.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file containing an entry with a crafted ACL that, when parsed by libarchive's archive_acl_from_text_l or similar functions, results in thousands of ACL entries being added via archive_acl_add_entry. Each entry contributes to the accumulated length in archive_acl_text_len. By carefully choosing the number and properties of entries (e.g., NFSv4 ACLs with long permission strings and flags as computed in lines 3550-3568), the attacker causes length to reach SIZE_MAX. When archive_acl_to_text_l is called (e.g., during archive_entry_acl_to_text at line 2986), the overflow occurs, malloc allocates a tiny buffer, and subsequent string operations write far beyond the allocated region, corrupting heap metadata and potentially achieving arbitrary code execution.

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/`:

- `bug_reports/archive_acl_text_len.overflow.10.json`
- `bug_reports/archive_acl_text_len.overflow.12.json`
- `bug_reports/strlen.pointer_arithmetic.5.json`
- `bug_reports/strlen.pointer_dereference.5.json`
- `bug_reports/unnamed_1779685656305.json`

## Reproduction

- **CBMC harness**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_l/harness.c`
- **Spec used**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_l/spec.json`
- **CBMC raw output**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_l/cbmc_result.json`
- **Classifier state**: `/tmp/libarchive_auto_v5_1779685630/v5/archive_acl/archive_acl_to_text_l/classification.json`





## Honest caveats (read before upstream reporting)

- **Dynamic outcome was `no_record`.** WEAK evidence: the dynamic harness did NOT reproduce the crash with the concrete CBMC witness. The realism LLM's vote is the only evidence.
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling pointer state"). **Independent code-level verification of that condition is required before reporting upstream.**
- Realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts; this report uses the strongest realistic record by mtime.
