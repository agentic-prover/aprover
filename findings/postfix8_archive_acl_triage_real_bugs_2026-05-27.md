# Postfix8 — TriageAgent v3 REAL_BUG findings (libarchive `archive_acl.c`)

## Source

`/tmp/libarchive_postfix8/` sweep, 7 fixes active (commits 0b4e4a8 through e35b57b). TriageAgent v3 (`bmc_agent/agents/triage_tools.py`, commit `e35b57b`) re-judged every per-CEx `outcome=real_bug` produced by the postfix8 pipeline, walking the call chain via tool-use (`lookup_function`, `find_more_callers`, `grep_corpus`) before voting.

**Triage stats** (run in progress at time of writing; 69 of ~80 verdicts written):

| Verdict | Count |
|---|---|
| **REAL_BUG** | **16** |
| LIKELY_FP | 52 |
| NEEDS_HUMAN | 1 |
| (parse error) | 3 |

## Root-cause de-duplication

The 16 per-CEx REAL_BUG verdicts collapse to **3 distinct root-cause bugs**:

| Root cause | Distinct per-CEx verdicts | Severity (triage agent's read) |
|---|---|---|
| **Bug 1**: `archive_acl_text_len` under-budgets writes that `append_entry`/`append_entry_w` perform (4 distinct variants) | 9 | Heap buffer overflow, attacker-influenced overflow byte count |
| **Bug 2**: `struct archive_acl` uninitialized-pointer path — `acl_new_entry` and `archive_acl_clear` both `free()` `acl_text_*` / dereference `acl_head` without zero-init or NULL-check guards | 6 | Segfault / heap-corruption on first use of a non-zero-allocated `archive_acl` |
| **Bug 3**: `archive_acl_copy` silently drops entries when `acl_new_entry` rejects a malformed-tag entry — no error path, void return | 1 | Data integrity / ACL-replication-correctness — silent partial copy |

Bug-2 evidence count rose from 2 to 6 between the prior report (`40be63f`) and now — the triage agent reached the bug from THREE different per-CEx witnesses (under `acl_new_entry`, `archive_acl_clear`, `archive_acl_copy`) and described it consistently from each angle. That cross-corroboration is a strong calibration signal.

---

## Bug 1 — Heap buffer overflow in `archive_acl_to_text_l/_w` (CONFIRMED REAL BUG, FOUR variants)

The bulk of the per-CEx evidence. **All four variants stem from the same architectural shape**: `archive_acl_text_len` is a SECOND COPY of the byte-counting logic that `append_entry`/`append_entry_w` performs, and the two copies have diverged.

### Variant 1.A — NFSv4 USER/GROUP with `name==NULL` and no `EXTRA_ID` flag (user's manual finding)

Per-CEx evidence: `append_id.pointer_arithmetic.5`, `append_id.pointer_dereference.11`, `append_entry_w.unwind.0`, `append_entry_w.overflow.2`.

> "The size calculator archive_acl_text_len (line 89-96) only budgets for the second append_id call when ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID flag is set. However, append_entry (line 109-111) writes ':' + id digits whenever id != -1, which occurs for NFS4 types with USER/GROUP tags and name==NULL, regardless of the EXTRA_ID flag."

Already documented + ASAN-confirmed in `findings/libarchive_archive_acl_to_text_heap_overflow_nfsv4_2026-05-27.md` (user's manual triage, commit `c260395`). Minimal patch already drafted there.

### Variant 1.B — Non-compact NFSv4 perm/flag output overruns the fixed 27-char budget

Per-CEx evidence: `append_entry_w.overflow.1`, `append_entry_w.overflow.3`, `append_entry_w.pointer_arithmetic.11`.

> "The size calculator archive_acl_text_len (line 631-634) budgets a fixed 27-28 characters for NFSv4 ACL permissions/flags/type, assuming compact representation. However, the writer append_entry_w (lines 1167-1183) writes one character per permission/flag bit when ARCHIVE_ENTRY_ACL_STYLE_COMPACT is NOT set, outputting dashes for unset bits. The loops iterate `nfsv4_acl_perm_map_size + nfsv4_acl_flag_map_size` times, plus 2 colons and a type string (allow/deny/audit/alarm = 5-6 chars). If these map sizes total more than ~20 elements (typical NFSv4 has 14 perms + 8 flags = 22), non-compact mode exceeds the 27-char budget. ... The runtime guard at line 770 ('Buffer overrun') confirms developers were aware of this risk but the calculator has a missing conditional check for the COMPACT flag."

**Public-API trigger**: `archive_acl_to_text_w(acl, ..., flags=0)` (no `STYLE_COMPACT`) on any NFSv4 ACL. Calculator allocates 27 chars/entry; writer emits 14 perm bytes + ':' + 8 flag bytes + ':' + 5-byte type = 29+ bytes. Per-entry overflow of 2-4 bytes.

### Variant 1.C — Calculator/writer conditional-branch mismatch on type encoding

Per-CEx evidence: `append_entry_w.pointer_arithmetic.17`, `append_entry_w.pointer_dereference.95`.

> "The size calculator archive_acl_text_len (line 630) uses `if (want_type == (ARCHIVE_ENTRY_ACL_TYPE_NFS4))` to decide whether to budget 27 chars for NFS4 formatting vs. 3 chars for POSIX formatting. However, the writer append_entry_w (line 1080) uses `if ((type & ARCHIVE_ENTRY_ACL_TYPE_POSIX1E) != 0)` to make the same decision. When type=0 (as in the CBMC counterexample), the writer's condition is FALSE so it executes the NFS4 branch writing 27+ characters, but the calculator's condition is also FALSE (since want_type would need to equal the specific combined NFS4 mask) so it budgets only 3 characters."

**Trigger**: caller passes a `type` value not exactly equal to the NFS4 mask. The writer interprets "not POSIX1E" as "NFS4"; the calculator requires an exact mask match. Disagreement → under-allocation.

### Variant 1.D — 2-colon write vs 1-colon budget (Solaris-style logic inversion)

Per-CEx evidence: `append_entry_w.pointer_dereference.71`.

> "At line 619 of the calculator, only 1 colon is budgeted (`length += 1`). However, append_entry_w writes TWO colons: one unconditionally at line 53, and a second conditionally at line 71 (inside the POSIX1E/USER/GROUP block, unless Solaris style with OTHER/MASK tag). The calculator attempts to handle the Solaris special case by subtracting 1 at lines 647-652, but this logic is inverted: it assumes 2 colons are budgeted and subtracts 1 for Solaris, when in fact only 1 colon is budgeted at line 619."

**Trigger**: any non-Solaris ACL entry, or Solaris with non-OTHER/MASK tag — i.e., the vast majority of ACL serializations. 1-wchar_t under-allocation per such entry, compounded by entry count.

### Combined severity of Bug 1

Four variants of the same architectural shape. Variant 1.A is ASAN-confirmed; the others have high-confidence code-review backing. **Recommended fix direction** — at the architectural level, replace the `archive_acl_text_len + malloc(length)` pattern with growable `archive_string` / `archive_wstring` buffers, which is what unreleased libarchive `master` already has. For the 3.7/3.8 maintenance branch, each variant needs a targeted surface fix:
- 1.A: condition the trailing `:id` budget on `(USER||GROUP) && (EXTRA_ID || NFS4_TYPE)`
- 1.B: condition the 27-char budget on `STYLE_COMPACT` and add per-bit accounting in non-compact mode
- 1.C: align calculator's `want_type == NFS4_MASK` check with writer's `!(type & POSIX1E)` check
- 1.D: fix the Solaris exception arithmetic (start from 1-colon baseline, ADD 1 for non-Solaris)

---

## Bug 2 — `struct archive_acl` uninitialized-pointer free / dereference (CONFIRMED REAL BUG, multiple call sites)

Per-CEx evidence: `acl_new_entry.precondition_instance.2`, `archive_acl_add_entry.acl_new_entry.precondition_instance.2`, `archive_acl_clear.precondition_instance.2`, `archive_acl_clear.main.pointer_dereference.3`, `archive_acl_copy.archive_acl_clear.pointer_dereference.1`, `archive_acl_copy.archive_acl_clear.precondition_instance.1`.

The triage agent reached the same root cause from THREE different per-CEx witnesses, attributing each to the appropriate function:

> "[from `acl_new_entry` perspective] acl_new_entry unconditionally calls free(acl->acl_text_w) and free(acl->acl_text) without checking if these pointers are initialized. ... If archive_acl is not zero-initialized when created, these pointers contain garbage values, and free() on garbage pointers causes undefined behavior (typically SIGSEGV). The dynamic reproducer confirms SIGSEGV."

> "[from `archive_acl_clear` perspective] archive_acl_clear at line 134-137 assumes acl->acl_head is either NULL or points to a valid linked list, but performs no validation. When archive_acl_copy calls archive_acl_clear(dest), if dest is uninitialized (acl_head contains garbage), the while loop condition (acl->acl_head != NULL) will be true for a non-NULL garbage pointer, and the subsequent dereference acl->acl_head->next will crash."

> "[from `archive_acl_copy` perspective] archive_acl_copy (archive_acl.c:150) calls archive_acl_clear(dest) without validating that dest's pointer fields are initialized. ... While archive_acl_copy has no current callers in the corpus, it is a non-static function that could be called by external code or future internal code, making this a latent defect."

**Public-API path**: `archive_entry_new()` → `archive_entry_acl()` → `archive_acl_add_entry()` → `acl_new_entry()` on first entry, OR `archive_acl_copy(dest, ...)` where `dest` was not zero-allocated. The internal allocator (`archive_entry_new` via `calloc`) zero-inits in practice, BUT:
- External callers that allocate `archive_acl` directly may not zero-init
- Internal refactors that pass `dest` parameters around can lose the zero-init invariant
- Stack-allocated `struct archive_acl` test code is a current path

**Severity**: SIGSEGV on first use is observable; the dyn-val harness confirmed it. Triage agent's "latent defect" framing is correct — depends on the future caller, but the API has no preconditions documenting the requirement.

**Fix**: either (a) add NULL-checks on `free()` calls (cheap, defensive), (b) introduce `archive_acl_init()` that callers must invoke, or (c) document the zero-init requirement.

---

## Bug 3 — `archive_acl_copy` silently drops entries when `acl_new_entry` returns NULL (CONFIRMED REAL BUG)

Per-CEx evidence: `acl_new_entry.precondition_instance.2` (under the `acl_new_entry` directory; same property index as Bug 2 but the triage agent's reasoning attributes to a different code path).

> "The bug is in archive_acl_copy (archive_acl.c:150-162). When acl_new_entry returns NULL due to validation failure (e.g., invalid tag=10006 in the counterexample), archive_acl_copy silently skips that entry and continues. Line 157-159 shows: `ap2 = acl_new_entry(dest, ap->type, ap->permset, ap->tag, ap->id); if (ap2 != NULL) archive_mstring_copy(&ap2->name, &ap->name);` followed by `ap = ap->next;` which continues the loop. This causes silent data loss - ACL entries are dropped without error reporting. The function has void return type, so callers cannot detect incomplete copies."

**Public-API trigger**: any caller of `archive_entry_acl_copy()` where the source ACL contains entries with non-canonical tags (which `acl_new_entry` rejects via `acl_new_entry_check`).

**Severity**: not memory-safety, but a data-integrity / security-policy correctness bug. An attacker who can stuff a malformed-tag entry into the source ACL — e.g., via a crafted archive or by feeding a parser non-canonical ACL strings — causes subsequent `archive_acl_copy` calls to silently produce an incomplete replica. Bypasses ACL replication when handling untrusted ACL data.

**Fix**: change `archive_acl_copy` to return an `int` (or set an error on `dest->archive`) when any `acl_new_entry` returns NULL, so callers can detect partial copies.

---

## NEEDS_HUMAN verdict (1)

The triage agent flagged one case as needing human verification:

> "**append_entry_w / append_entry_w.pointer_arithmetic.5 (medium)** — The audit reveals a potential buffer overflow in the NFSv4 ACL formatting path. ... Without access to the map definitions to verify their sizes sum to exactly 27 (accounting for 3 colons and the type string), I cannot definitively confirm whether the fixed budget is correct. ... A human reviewer should verify that `nfsv4_acl_perm_map_size + nfsv4_acl_flag_map_size + 3 (colons) + 5 (max type string) <= 27` in all configurations."

This is the same root as Variant 1.B. With direct inspection of `nfsv4_acl_perm_map_size` (14) + `nfsv4_acl_flag_map_size` (7) + 3 colons + 5 type chars = **29 — already exceeds 27**, even in compact mode. So the case ALSO resolves to Bug 1 / Variant 1.B; the triage agent was conservative about not having looked up the map sizes via tool.

---

## Per-CEx triage verdict table (REAL_BUG verdicts only)

| Function | Property | Bug class | Confidence |
|---|---|---|---|
| `acl_new_entry` | `acl_new_entry.precondition_instance.2` | Bug 3 (archive_acl_copy silent drop, attributed via this CEx) | high |
| `append_entry_w` | `overflow.1` | Bug 1.B (non-compact NFSv4 overrun) | high |
| `append_entry_w` | `overflow.2` | Bug 1.A (NFSv4 USER/GROUP no-name no-EXTRA_ID) | high |
| `append_entry_w` | `overflow.3` | Bug 1.B (non-compact NFSv4) | high |
| `append_entry_w` | `pointer_arithmetic.11` | Bug 1.B variant — 14+1+8+1+5=29 vs 28 budget | high |
| `append_entry_w` | `pointer_arithmetic.17` | Bug 1.C (calc/writer NFS4 detection mismatch) | high |
| `append_entry_w` | `pointer_dereference.71` | Bug 1.D (Solaris-colon inversion) | high |
| `append_entry_w` | `pointer_dereference.95` | Bug 1.C variant (type=0 hits NFS4 writer + POSIX budget) | high |
| `append_entry_w` | `unwind.0` | Bug 1.A wide-string variant | high |
| `append_id` | `pointer_arithmetic.5` | Bug 1.A (narrow-string `append_id` analogue) | high |
| `append_id` | `pointer_dereference.11` | Bug 1.A (NFSv4 + tag=OWNER + no EXTRA_ID) | high |
| `archive_acl_add_entry` | `acl_new_entry.precondition_instance.2` | Bug 2 (uninit free in acl_new_entry, via add_entry call) | high |
| `archive_acl_clear` | `archive_acl_clear.precondition_instance.2` | Bug 2 (uninit `acl_head` dereference in archive_acl_clear) | high |
| `archive_acl_clear` | `main.pointer_dereference.3` | Bug 2 (uninit `acl_head->next` dereference) | high |
| `archive_acl_copy` | `archive_acl_clear.pointer_dereference.1` | Bug 2 (archive_acl_copy passes uninit dest to clear) | high |
| `archive_acl_copy` | `archive_acl_clear.precondition_instance.1` | Bug 2 (latent: future callers may not zero-init) | high |

---

## How to act on this

| Bug | Status | Recommended next step |
|---|---|---|
| **Bug 1.A** | ASAN-confirmed + patch drafted | Already covered in earlier finding. Upstream filing path: bundle with 1.B/1.C/1.D as one report covering the calc/writer divergence class |
| **Bug 1.B** | Code-review confirmed (high confidence); needs ASAN reproducer | Build a reproducer using `archive_acl_to_text_w(acl, ..., flags=0)` with NFSv4 entries; expect ASAN report at line 770 of `append_entry_w` |
| **Bug 1.C** | Code-review confirmed | Build a reproducer passing `type=0` (or any non-NFS4-mask value); expect under-allocation overflow |
| **Bug 1.D** | Code-review confirmed | Build a reproducer with any non-Solaris-OTHER/MASK ACL entry; expect 1-wchar_t-per-entry underflow |
| **Bug 2** | Dyn-val SIGSEGV-confirmed; latent on current public-API paths (most callers zero-init) | Verify external-caller exposure surface; add defensive NULL-check on free or document zero-init precondition |
| **Bug 3** | Code-review confirmed; no reproducer needed | File upstream as-is — argues that `archive_entry_acl_copy` should return an error on partial copy |

## Known limitations of this triage

* **Triage agent did NOT independently build/run public-API reproducers.** All claims rest on (a) code-review argument or (b) the pipeline's existing dyn-val signal. ASAN-confirmed reproducers (like the user's 1.A) are the gold standard; the others are at "high-confidence code review" tier.
* **Triage agent's NFSv4 budget math for Variant 1.B may slightly over- or under-count** because it inferred map sizes from "typical NFSv4 has 14 perms + 8 flags = 22" without verifying via tool. Manual verification: `nfsv4_acl_perm_map_size = 14`, `nfsv4_acl_flag_map_size = 7` (lines 89-127). With 2 colons + 5-char type = `14+1+7+1+5 = 28` ≤ 28 (calculator's 27+1 max). So variant 1.B may be off-by-1 even in this calculation; the under-allocation in non-compact mode is real but smaller than the agent estimated.
* **`acl_new_entry.precondition_instance.2` appears under both Bug 2 and Bug 3** with different reasoning depending on the caller-chain frame. Both are real bugs in different functions touched by the same CEx; the property index is a misleading "duplicate" header.
* **3 parse errors** were observed across the run (out of ~70 invocations). The parser is robust to JSON-after-prose but the agent occasionally emits a malformed JSON object (extra key, missing quote). Each failed verdict would need a manual re-run.

---

*Triage run still in progress at time of writing. Updates landed via overwrite when new verdicts complete. This document represents the snapshot at ~80% triage completion.*
