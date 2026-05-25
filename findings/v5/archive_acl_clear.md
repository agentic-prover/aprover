# bmc-agent-sec confirmed finding: `archive_acl_clear`

**Status**: realism-confirmed (any CEx with `realism.verdict == realistic AND confidence != unlikely` makes the function confirmed).
**Generated**: 2026-05-25T06:03:11.207990+00:00

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_clear` (lines 129-147)
- **Violated property**: `archive_acl_clear.pointer_dereference.81` (CBMC-reported)
- **Call chain established**: `archive_acl_copy -> archive_acl_clear`

## bmc-agent-sec layered verdict

| Layer | Result |
|---|---|
| CBMC | counterexample found at property above |
| Realism (LLM auditor, primary call) | **realistic** / confidence `medium` |
| Dynamic harness (GCC + signal handlers) | **not_triggered**, signal=`None` |
| Final tier | `confirmed_system_entry` |

## Realism reasoning

The violation occurs at line 3169 where `free(acl->acl_text_w)` is called. The CBMC counterexample shows `acl->acl_text_w` pointing to a deallocated object (`__CPROVER_deallocated`). Looking at the call chain, `archive_acl_copy` (line 3178-3191) calls `archive_acl_clear(dest)` at line 3181. The function `archive_acl_clear` (lines 3160-3176) frees `acl->acl_text_w` at line 3169 and sets it to NULL at line 3170. However, the counterexample shows that `acl->acl_text_w` contains a pointer to deallocated memory BEFORE entering `archive_acl_clear`. This means the `dest` ACL structure passed to `archive_acl_copy` was already corrupted with a dangling pointer. An attacker could trigger this by: (1) Creating an archive_acl structure, (2) Calling operations that allocate `acl_text_w` (like `archive_acl_to_text_w`), (3) Manually freeing the memory or causing it to be freed through some API call sequence, (4) NOT nulling out the pointer (violating the API contract), (5) Then calling `archive_acl_copy` with this corrupted structure as `dest`. While this requires the caller to violate API contracts by creating a use-after-free scenario, libarchive is a library that processes untrusted archive data. If the library itself has a bug elsewhere that creates this dangling pointer state in normal operation (e.g., a double-free path, or failing to NULL a pointer after free in some error path), or if the library exposes the `archive_acl` structure to user manipulation in a way that allows pointer corruption, then this becomes exploitable. The CBMC trace showing `acl_p` also pointing to a stack object suggests potential memory corruption or API misuse. Given that archive libraries routinely handle malicious inputs and the witness shows a reachable state where pointers are already corrupted, this is REALISTIC.

## Exploit scenario (LLM-supplied)

An attacker crafts a malicious archive file that triggers a specific sequence of ACL parsing operations. During processing, the library allocates `acl_text_w` through `archive_acl_to_text_w`, then encounters a parsing error or specially-crafted data that causes an early free of this memory without properly nulling the pointer (due to a bug in error handling). Subsequently, the archive format requires copying ACL data, triggering `archive_acl_copy`. When this calls `archive_acl_clear` on the destination ACL (which still contains the dangling pointer from the earlier corruption), it attempts to free already-freed memory, causing a double-free vulnerability exploitable for arbitrary code execution.

## CBMC counterexample witness

The variable assignments CBMC reports as triggering the violation. Read with the function source below to understand the attack state:

```text
  __CPROVER_alloca_object = NULL
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = {'name': 'unknown'}
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = NULL
  __CPROVER_new_object = NULL
  __CPROVER_rounding_mode = 0
  __acl_obj_acl_head_obj = <struct: 6 members>
  __acl_obj_acl_head_obj.next = ((struct archive_acl_entry *)NULL)
  __acl_obj_acl_p_obj = <struct: 6 members>
  __acl_obj_acl_p_obj.next = ((struct archive_acl_entry *)NULL)
  __acl_obj_acl_text_buf = <array: 5 elements>
  __acl_obj_acl_text_buf[0l] = 0
  __acl_obj_acl_text_buf[1l] = 0
  __acl_obj_acl_text_buf[2l] = 0
  __acl_obj_acl_text_buf[3l] = 0
  __acl_obj_acl_text_buf[4l] = 0
  __acl_obj_acl_text_len = 0u
  _acl_obj = <struct: 10 members>
  _acl_obj.acl_head = ((struct archive_acl_entry *)NULL)
  _acl_obj.acl_p = __acl_obj_acl_p_obj!0@1
  _acl_obj.acl_text = __acl_obj_acl_text_buf!0@1
  acl = _acl_obj!0@1
  ap = ((struct archive_acl_entry *)NULL)
  ptr = {'name': 'unknown'}
  return_value___VERIFIER_nondet___CPROVER_bool = True
```

## Function source (from the snapshot)

```c
void
archive_acl_clear(struct archive_acl *acl)
{
	struct archive_acl_entry *ap;

	while (acl->acl_head != NULL) {
		ap = acl->acl_head->next;
		archive_mstring_clean(&acl->acl_head->name);
		free(acl->acl_head);
		acl->acl_head = ap;
	}
	free(acl->acl_text_w);
	acl->acl_text_w = NULL;
	free(acl->acl_text);
	acl->acl_text = NULL;
	acl->acl_p = NULL;
	acl->acl_types = 0;
	acl->acl_state = 0; /* Not counting. */
}
```

## Per-CEx history

The pipeline ran CBMC multiple times on this function (different failing properties, feedback-loop iterations). Each CEx has its own audit record under `bug_reports/` in the sweep artifact tree:

- `bug_reports/archive_acl_clear.pointer_dereference.81.json`
- `bug_reports/archive_acl_clear.precondition_instance.2.json`
- `bug_reports/unnamed_1779685656363.json`
- `bug_reports/unnamed_1779686140106.json`

## Reproduction

The harness CBMC verified is committed alongside this report as `harness.c`. To re-verify just this finding:

```bash
# 1. clone libarchive at the snapshot the sweep used
cd /tmp && git clone https://github.com/libarchive/libarchive
cd libarchive && git checkout 67830f7b9c27080c0170bcd71d94fb42316c47dd

# 2. apply CBMC bounds + pointer + signed-overflow checks
cbmc \
    --bounds-check --pointer-check --div-by-zero-check \
    --signed-overflow-check --unsigned-overflow-check --pointer-overflow-check \
    --unwind 4 --timeout 60 \
    -I /tmp/libarchive/libarchive -I /tmp/libarchive/libarchive/build \
    -DHAVE_CONFIG_H \
    --function main \
    archive_acl_clear/harness.c
# (paste the harness contents from the section below into harness.c first;
#  it is also committed alongside this report as harness.c.)

```

To re-run the full sweep end-to-end (re-derives this finding from scratch):

```bash
(no command provided)
```

## Honest caveats (read before upstream reporting)

- **Dynamic outcome was `not_triggered`.** WEAK evidence: the dynamic harness did NOT reproduce the crash with the concrete CBMC witness. The realism LLM's vote is the only evidence.
- The realism LLM's attacker scenario may hypothesize an upstream condition (e.g. "some bug elsewhere creates the dangling pointer state"). **Independent code-level verification of that condition is required before reporting upstream.**
- Realism nondeterminism: the same CEx can flip between REALISTIC and UNREALISTIC across runs. Multiple per-CEx records in `bug_reports/` may show different verdicts; this report uses the strongest realistic record by mtime.
- The harness is auto-generated and uses CBMC's nondeterministic-input model. Reading `harness.c` shows exactly what input states CBMC was free to explore — verify those states are actually reachable from the real public API before declaring a vulnerability.
