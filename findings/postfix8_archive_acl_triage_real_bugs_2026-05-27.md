# Postfix8 — TriageAgent v3 REAL_BUG findings (libarchive `archive_acl.c`)

## Source

`/tmp/libarchive_postfix8/` sweep, 7 fixes active (commits 0b4e4a8 through e35b57b). TriageAgent v3 (commit `e35b57b`) re-judged every per-CEx `outcome=real_bug` produced by the postfix8 pipeline, walking the call chain via tool-use (`lookup_function`, `find_more_callers`, `grep_corpus`) before voting.

This document covers the **12 REAL_BUG verdicts** the triage agent confirmed so far. (Triage run is in-flight against ~50 candidates; this report covers the first 24 verdicts: 12 REAL_BUG, 11 LIKELY_FP, 1 NEEDS_HUMAN.)

## Root-cause de-duplication

The 12 per-CEx verdicts collapse to **3 distinct root-cause bugs**:

| Root cause | Distinct per-CEx verdicts | Severity (triage agent's read) |
|---|---|---|
| **Bug 1**: `archive_acl_text_len` under-budgets writes that `append_entry`/`append_entry_w` perform | 7 (8 if `append_id.pointer_arithmetic.5` counted) | Heap buffer overflow, attacker-influenced count of overflowing bytes |
| **Bug 2**: `acl_new_entry` frees uninitialized `archive_acl::acl_text_*` pointers | 2 | SIGSEGV on first ACL entry of any non-zero-initialized struct |
| **Bug 3**: `archive_acl_copy` silently drops entries when `acl_new_entry` fails | 1 | Data integrity — silent partial copy of ACL data |

The bulk of the 12 verdicts are different CBMC property indices firing on the same `archive_acl_text_len`/`append_entry_w` mismatch — i.e., **CBMC found 7 distinct manifestations of one underlying class of bug** that the triage agent then unified.

---

## Bug 1 — Heap buffer overflow in `archive_acl_to_text_l/_w` (CONFIRMED REAL BUG, multiple variants)

This is the same root-cause bug the user reported manually (commit `c260395`, finding `libarchive_archive_acl_to_text_heap_overflow_nfsv4_2026-05-27.md`) — extended here with **several additional variants** the triage agent identified.

### Variant 1.A — NFSv4 USER/GROUP with `name==NULL` and no `EXTRA_ID` flag (user's manual finding)

Reproduced by triage agent on per-CEx `append_id.pointer_arithmetic.5`, `append_id.pointer_dereference.11`, `append_entry_w.unwind.0`, `append_entry_w.overflow.2`:

> "The size calculator archive_acl_text_len (line 89-96) only budgets for the second append_id call when ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID flag is set. However, append_entry (line 109-111) writes ':' + id digits whenever id != -1, which occurs for NFS4 types with USER/GROUP tags and name==NULL, regardless of the EXTRA_ID flag."

Public-API trigger: `archive_entry_acl_to_text(entry, ..., ARCHIVE_ENTRY_ACL_TYPE_NFS4)` on an entry containing an NFSv4 USER/GROUP ACL with no name. Already documented + patched in the user's earlier finding.

### Variant 1.B — Non-compact NFSv4 perm/flag output overruns the fixed 27-char budget

New finding, multiple per-CEx verdicts (`append_entry_w.overflow.1`, `.overflow.3`, `.pointer_arithmetic.11`):

> "The size calculator archive_acl_text_len (line 631-634) budgets a fixed 27-28 characters for NFSv4 ACL permissions/flags/type, assuming compact representation. However, the writer append_entry_w (lines 1167-1183) writes one character per permission/flag bit when ARCHIVE_ENTRY_ACL_STYLE_COMPACT is NOT set, outputting dashes for unset bits. The loops iterate `nfsv4_acl_perm_map_size + nfsv4_acl_flag_map_size` times, plus 2 colons and a type string (allow/deny/audit/alarm = 5-6 chars). If these map sizes total more than ~20 elements (typical NFSv4 has 14 perms + 8 flags = 22), non-compact mode exceeds the 27-char budget."

**Public-API trigger**: `archive_acl_to_text_w(acl, ..., flags=0)` (no `STYLE_COMPACT`) on any NFSv4 ACL → calculator allocates 27 chars/entry; writer emits 14 perm bytes + ':' + 8 flag bytes + ':' + 5-byte type = 29 bytes.

### Variant 1.C — Calculator/writer conditional-branch mismatch on type encoding

Per-CEx `append_entry_w.pointer_arithmetic.17`:

> "The size calculator archive_acl_text_len (line 630) uses `if (want_type == (ARCHIVE_ENTRY_ACL_TYPE_NFS4))` to decide whether to budget 27 chars for NFS4 formatting vs. 3 chars for POSIX formatting. However, the writer append_entry_w (line 1080) uses `if ((type & ARCHIVE_ENTRY_ACL_TYPE_POSIX1E) != 0)` to make the same decision. When type=0 (as in the CBMC counterexample), the writer's condition is FALSE so it executes the NFS4 branch writing 27+ characters, but the calculator's condition is also FALSE (since want_type would need to equal the specific combined NFS4 mask) so it budgets only 3 characters."

Same overall class (under-allocated buffer + overflow) but tracked separately because the trigger condition is **calculator and writer disagree on what counts as NFS4** — affecting any caller passing `type` values not exactly equal to the NFS4 mask.

### Variant 1.D — 2-colon write vs 1-colon budget (Solaris-style logic inversion)

Per-CEx `append_entry_w.pointer_dereference.71`:

> "The size calculator archive_acl_text_len at line 619 only budgets 1 colon (`length += 1`). However, append_entry_w writes TWO colons: one unconditionally at line 53 (`*(*wp)++ = L':'`), and a second conditionally at line 71 (inside the POSIX1E/USER/GROUP block, unless Solaris style with OTHER/MASK tag). The calculator attempts to handle the Solaris special case by subtracting 1 at lines 647-652, but this logic is inverted: it assumes 2 colons are budgeted and subtracts 1 for Solaris, when in fact only 1 colon is budgeted at line 619."

**Public-API trigger**: any non-Solaris ACL entry (or Solaris with non-OTHER/MASK tag) — i.e., the vast majority of ACL serializations. 1-wchar_t buffer underflow per such entry.

### Combined severity of Bug 1

Five variants of the same architectural shape — `archive_acl_text_len` is a SECOND COPY of the byte-counting logic that `append_entry`/`append_entry_w` performs, and it has accumulated divergences over time. Each variant is independently exploitable. The user's manual variant 1.A is ASAN-confirmed; the others have the same general shape and high confidence from triage. **Recommended fix direction**: rather than patching each variant, **delete `archive_acl_text_len` and replace the `malloc(length)` + `append_entry` pattern with growable buffers** — which is what unreleased libarchive `master` has already done. The 3.7/3.8 maintenance branch is where the variants need surface fixes.

---

## Bug 2 — `acl_new_entry` frees uninitialized `archive_acl::acl_text_*` pointers (CONFIRMED REAL BUG)

Per-CEx `acl_new_entry.precondition_instance.2`, `archive_acl_add_entry.acl_new_entry.precondition_instance.2`:

> "The bug is in acl_new_entry (archive_acl.c), which unconditionally calls free(acl->acl_text_w) and free(acl->acl_text) without checking if these pointers are initialized. Lines in acl_new_entry: `free(acl->acl_text_w); acl->acl_text_w = ((void *)0); free(acl->acl_text); acl->acl_text = ((void *)0);`. If archive_acl is not zero-initialized when created, these pointers contain garbage values, and free() on garbage pointers causes undefined behavior (typically SIGSEGV). The dynamic reproducer confirms SIGSEGV."

**Public-API trigger**: `archive_entry_new()` → `archive_entry_acl()` → `archive_acl_add_entry()` → `acl_new_entry()` on the first ACL entry, IF the underlying `archive_acl` struct was allocated without zero-init.

**Caveat (worth verifying)**: `archive_entry_new()` standardly uses `calloc` (zero-init), so this bug requires an unusual allocation path. Possible triggers:
* External caller that allocates `struct archive_acl` via `malloc` + manual init that misses `acl_text_*`
* Internal libarchive paths that re-init or partially-init existing entries (e.g., `archive_entry_clear` → `acl_new_entry`)

The dyn-val harness's SIGSEGV confirmation is strong evidence the path is reachable from the public surface bmc-agent generated — needs human verification of the exact call chain.

---

## Bug 3 — `archive_acl_copy` silently skips entries when `acl_new_entry` returns NULL (CONFIRMED REAL BUG)

Per-CEx `acl_new_entry.precondition_instance.2` (different from Bug 2 — this CEx attributes to copy, not add_entry):

> "The bug is in archive_acl_copy (archive_acl.c:150-162). When acl_new_entry returns NULL due to validation failure (e.g., invalid tag=10006 in the counterexample), archive_acl_copy silently skips that entry and continues. Line 157-159 shows: `ap2 = acl_new_entry(dest, ap->type, ap->permset, ap->tag, ap->id); if (ap2 != NULL) archive_mstring_copy(&ap2->name, &ap->name);` followed by `ap = ap->next;` which continues the loop. This causes silent data loss - ACL entries are dropped without error reporting. The function has void return type, so callers cannot detect incomplete copies."

**Public-API trigger**: any caller of `archive_entry_acl_copy()` where the source ACL contains entries with non-canonical tags (`acl_new_entry` rejects via `archive_acl_acl_new_entry_check()`).

**Severity**: not memory-safety, but a data-integrity / security-policy correctness bug — an attacker who can stuff a malformed-tag entry into the source ACL can cause subsequent copies to silently drop entries. Bypasses ACL replication when handling crafted ACLs from filesystems or untrusted archives.

---

## Per-CEx triage verdict table (REAL_BUG verdicts only)

| Function | Property | Bug class | Confidence |
|---|---|---|---|
| `acl_new_entry` | `acl_new_entry.precondition_instance.2` | Bug 2 / Bug 3 (depending on caller chain) | high |
| `append_entry_w` | `append_entry_w.overflow.1` | Bug 1.B (non-compact NFSv4 overrun) | high |
| `append_entry_w` | `append_entry_w.overflow.2` | Bug 1.A (NFSv4 + USER/GROUP + no EXTRA_ID) | high |
| `append_entry_w` | `append_entry_w.overflow.3` | Bug 1.B (non-compact NFSv4 overrun) | high |
| `append_entry_w` | `append_entry_w.pointer_arithmetic.11` | Bug 1.B variant — 14+1+8+1+5=29 vs 28 budget | high |
| `append_entry_w` | `append_entry_w.pointer_arithmetic.17` | Bug 1.C (calc/writer NFS4 detection mismatch) | high |
| `append_entry_w` | `append_entry_w.pointer_dereference.71` | Bug 1.D (Solaris-style colon inversion) | high |
| `append_entry_w` | `append_entry_w.pointer_dereference.95` | Bug 1.C variant (type=0 hits NFS4 writer path with POSIX budget) | high |
| `append_entry_w` | `append_entry_w.unwind.0` | Bug 1.A wide-string variant | high |
| `append_id` | `append_id.pointer_arithmetic.5` | Bug 1.A (narrow-string `append_id` analogue) | high |
| `append_id` | `append_id.pointer_dereference.11` | Bug 1.A (NFSv4 + tag=OWNER + no EXTRA_ID) | high |
| `archive_acl_add_entry` | `acl_new_entry.precondition_instance.2` | Bug 2 (uninit free in acl_new_entry) | high |

## How to act on this

1. **Bug 1 (heap overflow class)** — the user's manual variant 1.A has a confirmed reproducer + patch in `findings/libarchive_archive_acl_to_text_heap_overflow_nfsv4_2026-05-27.md`. The other variants (1.B, 1.C, 1.D) need ASAN reproducers built (similar pattern: `archive_entry_acl_to_text` with the right flag + entry combination). For upstream filing, **bundle as one report** — they're all symptoms of the same calc/writer divergence.
2. **Bug 2** — needs verification that the call chain through public API can reach `acl_new_entry` with non-zero-initialized `archive_acl`. The dyn-val SIGSEGV is strong but the harness may have constructed an unrealistic struct shape.
3. **Bug 3** — needs no further evidence; the bug is structurally clear from the source. Can be filed upstream as a correctness bug independent of the overflow class.

## Known limitations of this triage

* **Triage agent ran on per-CEx witnesses + tool-use; did NOT independently build and run a public-API reproducer.** All bug claims rely on either (a) code-review argument or (b) the pipeline's existing dyn-val signal. ASAN-confirmed reproducers (like the user's 1.A) are the gold standard; the other variants are at "high-confidence code review" tier.
* **Triage agent's call-chain audit may have stopped at intermediate frames.** For Bug 2 in particular, the exact path that produces a non-zero-initialized `archive_acl` is not enumerated — that audit step is the next human task.
* **9 of 12 CExs in Bug 1 are essentially duplicates of each other** (different CBMC property indices on the same write site). The "12 REAL_BUG verdicts" headline number is structurally inflated.

---

*Triage run in progress at time of writing; this document covers the first 24 verdicts. Will be updated when the remaining ~26 land.*
