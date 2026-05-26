# Bug report: `archive_match_exclude_entry` — add_entry.pointer_dereference.32

**Evidence grade**: **C** — judge-only (llm_no_reproducer)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_match.c`
- **Function**: `archive_match_exclude_entry`
- **Violated property**: `add_entry.pointer_dereference.32` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `add_entry.pointer_dereference.32` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `llm_no_reproducer` (signal `-`) |

## Judge reasoning

The bug is real. In add_entry (archive_match.c:1404-1418), when __archive_rb_tree_insert_node returns 0 (indicating the node already exists), the code calls __archive_rb_tree_find_node to retrieve the existing node. However, the code only checks if f2 != NULL at line 1418 AFTER already dereferencing it would occur. The problem is that __archive_rb_tree_find_node is an external function (declared in archive_rb.h but not defined in the corpus), and CBMC correctly models it as potentially returning an invalid pointer. The witness shows that when r=0 (insert failed), __archive_rb_tree_find_node can return a non-NULL but invalid pointer, causing the dereference at line 1419 (f2->flag) to fail. The check at line 1418 happens too late - the code structure suggests the developers expected find_node to always succeed after insert fails, but there's no guarantee. This is exploitable through the public API archive_match_exclude_entry when called with a valid archive_match object and entry, if the rb-tree implementation can return invalid pointers in edge cases.

## Exploit scenario (LLM-supplied)

An attacker calls archive_match_exclude_entry with a crafted archive_entry that causes __archive_rb_tree_insert_node to fail (return 0) and __archive_rb_tree_find_node to return an invalid pointer. This could happen if the rb-tree is in an inconsistent state or if there's a race condition. The invalid pointer dereference at f2->flag (line 1419) would cause a crash or potentially exploitable memory corruption.

### Source (`archive_match.c` starting at line 985)

```c
archive_match_exclude_entry(struct archive *_a, int flag,
    struct archive_entry *entry)
{
	struct archive_match *a;
	int r;

	archive_check_magic(_a, ARCHIVE_MATCH_MAGIC,
	    ARCHIVE_STATE_NEW, "archive_match_time_include_entry");
	a = (struct archive_match *)_a;

	if (entry == NULL) {
		archive_set_error(&(a->archive), EINVAL, "entry is NULL");
		return (ARCHIVE_FAILED);
	}
	r = validate_time_flag(_a, flag, "archive_match_exclude_entry");
	if (r != ARCHIVE_OK)
		return (r);
	return (add_entry(a, flag, entry));
}
```
### CBMC witness (variable assignments)

```text
(no witness assignments captured)
```

### Dynamic validation

- **Outcome**: `llm_no_reproducer`
- **Signal**: `-`
- **Sanitizer hit**: `None`
- **Attempts**: 1 (final attempt = 1)
- **Reproducer**: `-`

**Sanitizer output (excerpt)**:

```text

```


## Reproduction artifacts

- Harness: `/tmp/libarchive_judge_v7/judge_v7/archive_match/archive_match_exclude_entry/harness.c`
- CBMC result: `/tmp/libarchive_judge_v7/judge_v7/archive_match/archive_match_exclude_entry/cbmc_result.json`
- Per-CEx judge JSON: `/tmp/libarchive_judge_v7/judge_v7/archive_match/archive_match_exclude_entry/judge_add_entry.pointer_dereference.32.json`

## Caveats

- This is an *automated* finding. The CBMC counterexample is real (CBMC's
  proof obligation failed), but the call-chain feasibility from a public
  libarchive API has been argued by an LLM judge — not been independently
  exploited end-to-end except where the dynamic-reproduction grade is `A`.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may be added later. See `findings/v7/index.md`.
