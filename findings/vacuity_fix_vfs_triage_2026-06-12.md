# Vacuity fix on vfs.c: un-vacuuming surfaces real bugs (+ tree-model FPs)

Validation of the Step-1.5c vacuity fix (materialize init-trusted NULL globals
before the `!= NULL` assume) on the **fixed** `vfs.c` under `--agentic`, with the
de-anchored + threat-context-aware realism gate also active.

## Result

- **Before the fix (vacuous):** `mem_root`-referencing functions verified
  `assume(false)` -> 0 findings (fake-clean, UNSOUND — 13–17/27 functions).
- **After the fix:** real verification -> `28 real bug(s), 0 latent, 45 filtered`
  (73 CEXs total; gate filtered 45).

These 28 are a **mix**, not pure noise:

### Real bugs the vacuity was MASKING (confirmed by inspection)
- **`vfs_readdir` / `readdir_callback` — `name_size == 0` integer underflow → OOB
  write.** `name_size` is an API/syscall parameter (attacker-controlled per the
  VibeOS threat model). It flows unchecked into:
  - `strncpy(name, child->name, name_size - 1); name[name_size - 1] = '\0';`
  - `ctx.name_size = name_size` → `strncpy(ctx->name, name, ctx->name_size - 1)`
  When `name_size == 0`, `name_size - 1` wraps to `SIZE_MAX` (strncpy writes up to
  SIZE_MAX bytes) and `name[name_size - 1]` is `name[SIZE_MAX]` — a wild OOB
  write. No `name_size == 0` guard. CLEAR real bug, attacker-reachable.
- `vfs_write` / `vfs_append` — `overflow.*` / `memcpy.overflow.1` on
  attacker-controlled size arithmetic (plausible; needs the same param-reachable
  triage as above).

### Tree-model FPs (the noise to refine)
- `vfs_lookup`, `vfs_open_handle` — `pointer_dereference.*` /
  `pointer_arithmetic.*` reached through the materialized `mem_root`. The
  materialization is `calloc(1, sizeof(*g))` = a ZEROED tree (NULL children,
  `child_count == 0`, NULL parent), which is NOT the fully-linked tree
  `vfs_init` builds. Derefs of that incomplete tree are harness artifacts, not
  real bugs.

## Conclusion

The vacuity fix is **sound and valuable** — it closes a real soundness hole
(fake-clean verification) and surfaces real bugs that were silently masked. It
is committed on that basis.

Follow-up to cut the tree-model FP class (separate, supervised):
1. Model the materialized init-trusted struct-pointer global as a more realistic
   node (e.g. self-parent for a root, bounded child_count) instead of fully
   zeroed; OR
2. Add a realism-gate hint that flags a `pointer_dereference` whose witness walks
   a materialized (zeroed) init-trusted global's sub-structure as a
   harness-artifact (the existing `_witness_indicates_uninitialized_library`
   detector is the natural home).

The `name_size == 0` underflow in `vfs_readdir` should be reported upstream to
notgull/vibeos (a real fix: `if (name_size == 0) return -1;` before the copies).
