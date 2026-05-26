# Rescue results — 2026-05-24

Two LLM-driven validation passes against the existing N=3 sweep at
`/tmp/libarchive_n3_full_out/seedhunt_n3/`, routed through OpenRouter
→ `anthropic/claude-sonnet-4.5`. Both used the existing
`RealismChecker` with full source/witness/call-chain context.

## Pass 1 — re-realism on 23 API-blocked findings

`scripts/rerun_realism.py` re-checked every `bug_report.json` whose
original realism call failed (typically "workspace API usage limits").
Eligibility: original verdict=uncertain AND reasoning contained an
LLM-call-failure marker.

```
counts={'OK': 23, 'SKIP': 364, 'ERROR': 0}
verdicts={'unrealistic': 21, 'uncertain': 2}
```

**Zero new REALISTIC findings.** The 23 retried findings were
dominantly the wrong-struct-cast / bid-memcmp artifact pattern; the
LLM correctly recognized them as harness-permissiveness artifacts.

## Pass 2 — rescue on the 9 missed seed bugs

`scripts/rescue_spurious.py` runs the realism check with the pre-LLM
heuristic detectors monkey-patched out, so the LLM gets the full
context. Tested on classifications the pipeline normally drops
(outcome=spurious).

| Function | Commit | Class | Rescue verdict |
|---|---|---|---|
| `archive_acl_to_text_w` | `d45b5b4b` | A (unwind-filter) | **unrealistic** |
| `lzx_decode` | `79a0787b` | A | **unrealistic** |
| `lzx_huffman_init` | `1f545457` | B (caller-state) | **unrealistic** |
| `parse_rockridge` | `c3cb1c56` | B | **unrealistic** |
| `isJolietSVD` | `a9d2cc5e` | B | **unrealistic** |
| `do_uncompress_file` | `25d97315` | A | **unrealistic** |
| `init_unpack` | `620bdafa` | B | **unrealistic** |
| `__archive_pathmatch_w` | `4cbf9582` | A | **unrealistic** |
| `archive_match_path_excluded` | `470379a9` | C | (skip — was real_bug, already triaged in original pass) |

**0 of 8 rescued.** The LLM, with full context, agrees with the
classifier that none of the SPURIOUS CExes correspond to the actual
seed bug. The seed bugs lurk in *different* CEx slots that dedup
discarded or that the harness model cannot produce.

## Implications for the plan

Two earlier hypotheses are ruled out:

- ❌ **"The 30+ uncertain findings hide real bugs"** — Pass 1 shows 21
  of 23 are correctly-classified artifacts; the LLM gives no rescues.
- ❌ **"Routing SPURIOUS through realism would unlock missed seeds"**
  — Pass 2 shows 0 of 8 missed seeds rescue. The classifier was right.

What remains:

- ✅ **Expand corpus** — current 7 files give 5/14 seed-bug coverage
  inside the corpus. Adding tar/zip/7z/mtree/xar adds new commits
  and new bug-finding opportunities. Linear in files, low risk.
- ✅ **Harness-init for format-specific structs** (medium-term) —
  pre-initialize `a->format->data` to a properly-typed allocated
  struct for format-reader functions. May expose deeper CExes that
  the current harness mis-models. Uncertain payoff per the smoke
  test, but the only mechanism that could unlock Class-B seeds.

Tracking: corpus expansion goes first; harness-init is held back
until corpus expansion exhausts its leverage.
