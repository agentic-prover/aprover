# Autonomous-mode finding: `archive_acl_clear` double-free (TENTATIVE)

**Status**: `confirmed_dynamic` per bmc-agent-sec methodology, but **flagged for independent verification** — see "Skeptic Notes" below before reporting upstream.

**Generated**: 2026-05-25 by bmc-agent-sec autonomous run on libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`, b_start).

## Target

- **File**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_clear`
- **Violated property**: `archive_acl_clear.pointer_dereference.87` (CBMC)
- **Bug class**: double-free / use-after-free on `acl->acl_text`
- **Call chain established**: `archive_acl_copy → archive_acl_clear`

## Tool verdict (bmc-agent-sec)

| Layer | Result |
|---|---|
| CBMC | counterexample found (witness: `acl->acl_text = __CPROVER_deallocated`) |
| Reachability check | CBMC errored → LLM fallback established reachability |
| Feasibility check | skipped (BMC was exact for this property) |
| Dynamic validation (GCC + ASAN) | **SIGABRT** with double-free detection |
| Realism (Sonnet 4.5, minimal-attacker prompt) | **REALISTIC, high confidence** |
| **Final tier** | **`confirmed_dynamic`** (strongest evidence tier) |

## Realism reasoning (LLM summary)

The function frees `acl->acl_text` at line 3171 then nulls the pointer at line 3172. There is no guard `if (acl->acl_text != NULL)` before the free, and `free(NULL)` is well-defined safe — so the double-free only fires if `acl->acl_text` is **non-NULL and already-freed** when `archive_acl_clear` is invoked.

The LLM claims an attacker can drive the public API (e.g., `archive_acl_copy`, `archive_acl_add_entry`, `archive_acl_from_text_l`) into a state where `acl_text` points to freed memory but is not NULL, then calling `archive_acl_clear` triggers the double-free.

## Dynamic harness result

The GCC+ASAN harness driven with CBMC's concrete witness aborted with double-free detection on `acl_text`. This **confirms the bug class is real if the input state is reachable** — it does NOT independently confirm that the input state is reachable from any legal public-API sequence.

## Skeptic notes (READ BEFORE UPSTREAM REPORTING)

The verdict is `confirmed_dynamic`, but two failure modes from earlier in the session (`lzx_decode_free`, `lzx_read_pre_tree`) showed the same shape:

- CBMC harness directly creates a corrupted struct state (`field = __CPROVER_deallocated`)
- Function under test misbehaves when handed that state
- Dynamic harness, driven with the same corrupted state, crashes
- LLM hand-waves an attack scenario but doesn't quote a specific public-API sequence that produces the corrupted state

For both `lzx_decode_free` and `lzx_read_pre_tree`, offline grep-based analysis confirmed **no legal API sequence can produce the corrupted state** — they were CBMC harness artifacts.

**The same risk applies here.** Before reporting upstream, verify independently:

1. **grep all sites that `free()` `acl->acl_text`** and check whether ANY path frees without nulling. If only `archive_acl_clear` itself frees it (and always nulls after), the freed-but-not-nulled state is unreachable from public-API.

2. **Trace `archive_acl_add_entry`, `archive_acl_from_text_l`, error paths** for any place that frees `acl_text` without the matching `acl_text = NULL`.

3. **Look for struct-reuse paths**: if `archive_acl_clear` can be called on a struct that was previously freed (in a wrapping `archive_*_free` function) without `acl_text` being nulled, that's the exploitable state.

If steps 1-3 don't surface a real freed-but-not-nulled path, this finding is most likely a CBMC harness artifact and should NOT be reported upstream.

## Snapshot of the actual code (`archive_acl.c`, lines 130-147)

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
    free(acl->acl_text_w);      // line 140
    acl->acl_text_w = NULL;     // line 141
    free(acl->acl_text);        // line 142  <-- claimed double-free site
    acl->acl_text = NULL;       // line 143
    acl->acl_p = NULL;
    acl->acl_types = 0;
    acl->acl_state = 0;
}
```

## Historical-fix check

Searched libarchive git history for fixes to `archive_acl_clear` or `acl_text` double-free:

- **`4bcbb1b0`** (2016) — "Reset acl_types in archive_acl_clear()" — added `acl->acl_types = 0`; **not a double-free fix**.
- No other commits in libarchive history target `archive_acl_clear` for double-free / UAF.
- Not in the 43 documented seed-fix commits for the b_start..b_end interval.

**Therefore this is a NEW (latent) claim, not matching any known seed.** New does not mean correct — see Skeptic Notes.

## Reproduction

- Output dir: `/tmp/libarchive_auto_1779655235/auto/archive_acl/archive_acl_clear/`
- `bug_report.json` — full CBMC CEx + realism verdict
- `harness.c` — CBMC harness that produced the CEx

## Next steps

1. Perform the grep-based verification in Skeptic Notes.
2. If verified: report upstream as libarchive issue with this writeup.
3. If artifact: file under bmc-agent-sec's FP-pattern catalog and consider adding a learned-constraint to prevent re-discovery.
