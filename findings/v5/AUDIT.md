# External audit of v5 realism-endorsed findings

**Auditor**: human (project owner)
**Date**: 2026-05-25
**Scope**: the 9 realism-endorsed findings in this folder at audit time
(archive_be32enc came in after the audit and isn't covered here).

## Verdicts

| Finding | Auditor verdict | Notes |
|---|---|---|
| `next_field` | **Most plausible real bug** | `archive_acl_from_text_nl()` is length-based; `next_field()` can deref `**p` after `*l` reaches 0 if input lacks a separator/NUL inside the supplied length. Worth PoC + upstream report (OOB read / DoS). Matches seed-fix commit `8308b61c`. |
| `archive_acl_text_len` | Hardening-only | Theoretical `size_t` overflow, not practically attacker-reachable without impossible memory. |
| `archive_acl_to_text_l` | Hardening-only (duplicate of `archive_acl_text_len`) | Same root pattern. |
| `archive_acl_to_text_w` | **False positive** | Harness lets `archive_acl_text_len()` return impossible length=2; real code adds 31/32 for access ACL base entries. |
| `archive_acl_clear` | **False positive** | Harness frees stack / uninitialized / dangling pointers; no public-API path produces the freed-but-not-nulled state. |
| `archive_le32enc` | **False positive** | Direct NULL API misuse; requires caller-supplied writable 4-byte buffer. Not attacker-reachable. |
| `append_entry` | **False positive** | Invalid harness; arbitrary prefix / tiny buffer not reachable. |
| `append_entry_w` | **False positive** | NFSv4 length math is not undercounted as claimed; witness uses invalid ACL states. |
| `next_field_w` | **False positive** | No length-based buffer; requires NUL-terminated wide string, and the "backspace is whitespace" reasoning in the LLM scenario is wrong. |

## Net

| Bucket | Count |
|---|---|
| Plausible real bug (worth PoC + upstream) | **1** (`next_field`, matches seed `8308b61c`) |
| Hardening-only (theoretical) | 2 |
| False positives | 6 |
| **Total realism-endorsed at audit time** | **9** |

## What this tells us about bmc-agent-sec calibration

- Realism check's precision on this 9-finding sample: **1/9 = 11% true-positive rate** at the strict bar ("worth upstream report").
- Realism check's recall of the documented seed bug: **1/1** for `next_field` (8308b61c).
- The seed-bug recall is the encouraging signal — bmc-agent-sec did find the real one. The headline FP rate reflects that realism endorses plausible-looking patterns without independently verifying that the upstream condition is reachable from public API.

## Implications

1. **Realism alone is not enough.** Every realism-endorsed finding needs a grep-based code audit OR a successful PoC reproduction before upstream reporting. The Honest caveats section of each report already states this; the audit makes the rate concrete.
2. **Dynamic harness is the missing link.** `archive_le32enc` is the only finding tagged `confirmed_dynamic` in v5, but the audit calls it a false positive — the dynamic crash is on a harness that calls the function with NULL/invalid args, which a real caller would never do. So even dynamic-confirmed isn't sufficient on its own without "is the harness state reachable from public API?" verification.
3. **`next_field` is the win.** It's a documented seed bug that bmc-agent-sec found without prompt overfitting (no seed function names in the prompt). For evaluation purposes that's one real seed-bug match.
4. **The wired but not-yet-tested scenario-guided dynamic** (task #55) would attempt to translate the LLM's attacker_scenario into a concrete public-API reproducer. If the scenario is real, that should crash; if it's not, the LLM should bail with `// UNREPRODUCIBLE`. That mechanism should reduce the FP-via-impossible-harness rate substantially. v5 launched before that wiring; the next sweep will exercise it.

## Action items

- Treat `next_field` as a viable upstream candidate; build a PoC against current libarchive HEAD before filing.
- Mark the other 8 as `LLM-FP-audit-rejected` in any tally.
- Run the next sweep (v6) with scenario-guided dynamic on by default; measure how many findings get filtered by the LLM declining (`// UNREPRODUCIBLE`) vs. how many get promoted (PoC crashes confirm).
