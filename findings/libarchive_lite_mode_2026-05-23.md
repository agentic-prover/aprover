# BMC-Agent-Lite first sweep on libarchive — 2026-05-23

User-directed run of the new `--lite-mode` (bmc-agent-lite) feature
against the libarchive b_start snapshot
(`67830f7b9c27080c0170bcd71d94fb42316c47dd`). Companion to
`libarchive_sweep_b_start_2026-05-22.md`, which used the older custom
trivial-spec bench scaffold; this sweep is the first time the proper
`--lite-mode` flag (Phase 1 LLM-spec-gen skipped + Pass 1.5 skipped +
CBMC built-in checks + Phase 3 realism/classifier preserved) has been
run on real OSS code.

## TL;DR

* `--lite-mode` works end-to-end. On a single representative file
  (`archive_acl.c`) under `verify`, it lands **14 confirmed bugs**
  (13 MEMORY_SAFETY + 1 SEMANTIC) in ~10 min with the anthropic
  provider.
* The full directory sweep under `verify-dir` is essentially
  **blocked at the CBMC parse layer**: 4829 / 4842 CBMC runs
  (**99.7%**) exited with code 6 before any verdict was produced.
  Only `archive_rb.c` (13 runs) produced verdicts and the only 12
  confirmed bugs in the whole sweep.
* Root cause: cascading-typedef-strip discrepancy between the
  `verify` and `verify-dir` harness paths — `verify-dir` triggers
  `kernel_mode=False`, which strips system typedefs (`wint_t`,
  `wchar_t`, `mbstate_t`, …) but leaves the function declarations
  using them, producing `syntax error before '*'` at every harness
  parse.
* Two fixes pushed during the run improve coverage on the
  affected files (defines plumbing → `HAVE_CONFIG_H` reaches `cc -E`;
  cascade-strip → declarations referencing stripped typedefs are
  removed). The cascade fix unblocks the wchar/wint declarations but
  a follow-on `register_t defined twice` conflict still blocks
  `archive_acl.c` under `verify-dir`. Not whack-a-moled further in
  this session.

## Setup

* Clone: `/tmp/libarchive_bench/libarchive` @ `67830f7b9c…` (reused
  from the 2026-05-22 sweep).
* Build dir: `/tmp/libarchive_bench/libarchive/build` (cmake-configured
  for `config.h`).
* Command:

  ```bash
  BMC_AGENT_CBMC_TIMEOUT=60 uv run bmc-agent verify-dir \
    --source-dir /tmp/libarchive_bench/libarchive/libarchive \
    --driver libarchive_b_start_lite \
    --output /tmp/libarchive_lite \
    --include-dir /tmp/libarchive_bench/libarchive/build \
    --include-dir /tmp/libarchive_bench/libarchive/libarchive \
    --lite-mode --enable-realism-check --enable-dynamic-validation \
    --exclude 'test_*' --exclude 'read_open_memory.c' \
    -D HAVE_CONFIG_H
  ```

* CBMC 5.95.1, `--unwind 4 --bounds-check --pointer-check
  --signed-overflow-check`, 60-second timeout per function.
* Scope: 132 source `.c` files at the top level of `libarchive/` (the
  307 `test_*.c` files in `libarchive/test/` were excluded). 8 files
  failed Pass 1 preprocessing, leaving 124 files processed.
* Provider: anthropic API (single sweep used the leaked key the user
  revoked after this run).

## Aggregate

| Metric | Value |
|---|---:|
| Files processed | 124 |
| Files where at least one CBMC run produced a verdict | **1** |
| Files where 100% of CBMC runs failed at parse/convert | 123 |
| Total CBMC runs | 4842 |
| CBMC runs that produced a verdict | 13 |
| CBMC runs that failed before any verdict | **4829 (99.7%)** |
| Total confirmed bugs | **12** |

Per-file vs. the 2026-05-22 trivial-spec sweep on the same 13-file
subset:

| File | 2026-05-22 (VERIFIED+FAIL) | This sweep (verdicts produced) |
|---|---:|---:|
| archive_acl.c | 25 / 32 | **0 / 38** |
| archive_pathmatch.c | 8 / 8 | 0 / 8 |
| archive_match.c | 51 / 60 | 0 / 60 |
| archive_string.c | 78 / 81 | 0 / 81 |
| archive_read_support_format_rar5.c | 97 / 99 | 0 / 99 |
| archive_read_support_format_cab.c | 41 / 43 | 0 / 43 |
| archive_read_support_format_iso9660.c | 49 / 50 | 0 / 50 |
| archive_read_support_format_mtree.c | 35 / 36 | 0 / 36 |
| archive_read_support_format_cpio.c | 15 / 23 | 0 / 23 |
| archive_read_support_format_rar.c | 54 / 61 | 0 / 61 |
| archive_read_support_format_zip.c | 43 / 49 | 0 / 49 |

The 2026-05-22 sweep used an older harness path that didn't trigger
the typedef-strip cascade because it kept `wint_t` / `wchar_t` in the
harness. The new `--lite-mode` runs through the unified harness
generator, which under `verify-dir` strips those typedefs and breaks
every downstream `<wchar.h>` declaration. Coverage collapsed from
**~83 %** (2026-05-22) to **0.3 %** here.

## The 12 confirmed bugs (all in archive_rb.c)

archive_rb.c is the one file that survived because it uses only
`<stddef.h>` + project headers — no `<wchar.h>`, no `<inttypes.h>`.
The bugs are all in the generic RB-tree primitives, hit on a call
chain that traces back to a public-API entry point:

| Function | Property | Call chain |
|---|---|---|
| `__archive_rb_tree_insert_node` | pointer_dereference.7 | archive_match_exclude_entry → add_entry → … |
| `__archive_rb_tree_find_node` | pointer_dereference.7 | (via archive_match) |
| `__archive_rb_tree_find_node_leq` | pointer_dereference.7 | (via archive_match) |
| `__archive_rb_tree_find_node_geq` | pointer_dereference.7 | (via archive_match) |
| `__archive_rb_tree_prune_blackred_branch` | array_bounds.1 | (via archive_match) |
| `__archive_rb_tree_prune_blackred_branch` | pointer_dereference.13 | (via archive_match) |
| `__archive_rb_tree_iterate` | array_bounds.2 | (via archive_match) |
| `__archive_rb_tree_iterate` | pointer_dereference.25 | (via archive_match) |
| `__archive_rb_tree_remove_node` | pointer_dereference.7 | (via archive_match) |
| `__archive_rb_tree_remove_node` | array_bounds.1 | (via archive_match) |
| `__archive_rb_tree_reparent_nodes` | array_bounds.1 | (via archive_match) |
| `__archive_rb_tree_reparent_nodes` | pointer_dereference.13 | (via archive_match) |

**Triage caveat**: these are almost certainly the canonical
caller-contract-slip pattern the 2026-05-22 methodology note
described — under permissive lite-mode specs, the harness can
instantiate a tree with `tree->ops == NULL` or a node whose
`rbn_compare_nodes` callback is missing. Real callers
(`archive_match_exclude_entry`, etc.) always set those fields up
front, so the CEx state isn't reachable from the public API in
practice. The realism checker still tagged them REALISTIC, which is
a separate calibration finding: lite-mode + caller-contract slip is
a known weak spot for the realism prompt as it stands.

Artifacts: `/tmp/libarchive_lite/libarchive_b_start_lite/archive_rb/<func>/`.

## Fixes landed during the sweep

Two unrelated harness-generator/preprocessor bugs surfaced as soon
as `--lite-mode` met real OSS. Both pushed to `main`.

### 1. `-D` defines not plumbed into the Pass-1 call-graph preprocessor

`bmc_agent/pipeline.py` had two `preprocess()` call sites that
ignored `config.cbmc_defines`. `-D HAVE_CONFIG_H` reached CBMC but
not the `cc -E` Pass-1 expand, so every `archive_disk_acl_*.c` (and
any libarchive file gated on `HAVE_CONFIG_H`) preprocess-failed with
`#error Oops: No config.h and no built-in configuration`. Fix
threads `defines=list(config.cbmc_defines or [])` through both call
sites. Commit `0c8c023`.

### 2. Forward decls referencing stripped typedefs left orphaned

`bmc_agent/harness_generator.py::_strip_typedefs` strips
`__`-prefixed glibc-internal typedefs (`__gwchar_t`, etc.) and
C-standard typedefs in `_SYSTEM_TYPEDEF_NAMES` (`wint_t`,
`wchar_t`, `mbstate_t`, …), under the assumption that the harness
preamble's `#include <stdint.h>` / `<wchar.h>` provides them. But
the strip leaves behind the *declarations* that mention them:

```c
/* typedef wint_t removed */
...
extern wint_t btowc (int __c) ;       /* still here — CBMC parse error */
```

Two fixes:

* Initial pass (`0c8c023`): extend `_SYSTEM_FUNCTION_NAMES` with the
  small set of `<inttypes.h>` functions using `__gwchar_t`
  (`strtoimax`, `wcstoimax`, `imaxabs`, …).
* Cascade pass (this commit): scan the input text for
  `/* typedef X removed */` markers, build the stripped set
  automatically, and strip any forward decl (`extern …`) that
  references one of those names. Catches the entire `<wchar.h>` and
  `<stdio.h>` wide-char family in one shot.

The cascade fix alone removes ~120 broken forward decls per harness.
A single-file `verify-dir` test on `archive_acl.c` confirms the
cascade fires (every `wcs*`/`mbs*`/wide-char decl annotated as
`/* X decl removed: references stripped Y */`).

### Outstanding: `register_t defined twice`

After the cascade fix lands, the next CBMC parse error on
`archive_acl.c` is:

```
type symbol 'register_t' defined twice:
Original: signed long int
     New: register_t
```

Source: `archive_platform.h` (with `HAVE_CONFIG_H` set) typedefs
`register_t` to a different width than CBMC's built-in model
(`signed long int` on Linux x86_64). This is a libarchive-vs-CBMC
type-model conflict rather than a generic harness-gen bug, so it's
deferred — almost certainly a similar cascade will be needed for the
POSIX-types layer.

## Methodology notes

* **`--lite-mode` per-file works.** The `verify` (single-source)
  path on `archive_acl.c` lands 14 confirmed bugs in ~10 min with
  the anthropic provider. The `--lite-mode` mechanism (permissive
  specs + CBMC built-in checks + Phase 3 LLM only on CEx) does
  exactly what the design intends.
* **The unified harness-gen path is not yet ready for whole-OSS
  scale.** Each new codebase exposes a new typedef-strip / decl-
  strip / type-model edge case. The cascade-strip fix from this
  session is the right structural direction (don't hand-list every
  affected function; derive from the strip markers).
* **Lite-mode's realism check needs caller-contract-slip
  immunisation.** The 12 archive_rb findings are the textbook
  pattern — utility functions whose contracts are upheld by every
  real caller. The realism prompt should be told that `lite_mode`
  is in effect so it can apply stronger skepticism on functions
  with strong caller-contract expectations.
* **Methodology comparison:** the 2026-05-22 trivial-spec sweep
  (custom bench scaffold) and this lite-mode sweep test the same
  hypothesis (permissive PRE/POST + CBMC) on the same snapshot. The
  former achieved 83 % coverage; the latter 0.3 %. The delta is
  entirely in the harness path. Once the cascade and POSIX-types
  layers are fixed, lite-mode + the realism filter should produce a
  comparable raw coverage number with a higher-precision REAL_BUG
  set (the trivial-spec sweep had no realism filter at all).

## Next session

1. Fix the `register_t` / POSIX-types cascade (apply the same
   "strip decls that reference stripped types" rule across the
   `<sys/types.h>` layer, not just `<wchar.h>`/`<inttypes.h>`).
2. Re-run the sweep and compare per-file coverage against the
   2026-05-22 trivial-spec numbers as the headline calibration.
3. Triage the 12 archive_rb findings against libarchive's git
   history — RB-tree code is small, well-reviewed, and unlikely to
   have a NULL-deref CVE. Score them as caller-contract slips and
   feed the pattern into the realism-prompt revisions.

## Files

* Sweep log: `findings/libarchive_lite_mode_2026-05-23.log`
  (truncated head; full at `/tmp/libarchive_lite.log` locally).
* Per-file scorecards: `/tmp/libarchive_lite/libarchive_b_start_lite/<file>/`.
* archive_acl single-file calibration: `/tmp/libarchive_lite_test/test_acl/`.
