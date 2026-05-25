# bmc-agent-sec confirmed findings

**Generated**: 2026-05-25T06:32:55.824678+00:00  
**Sweep output**: `/tmp/libarchive_auto_v5_1779685630`  
**Driver**: `v5`

## Summary (10 realism-endorsed function(s))

A function is counted as confirmed if AT LEAST ONE of its CBMC counterexamples passed the realism check (verdict=realistic, confidence!=unlikely). Realism nondeterminism is real: see each report's per-CEx history for the audit trail.

| # | Function | File | Property | Tier | Realism | Dynamic |
|---|---|---|---|---|---|---|
| 1 | [`append_entry`](append_entry.md) | `archive_acl.c` | `strcpy.pointer_dereference.11` | `confirmed_system_entry` | realistic / high | no_record |
| 2 | [`append_entry_w`](append_entry_w.md) | `archive_acl.c` | `append_entry_w.pointer_dereference.71` | `confirmed_system_entry` | realistic / high | no_record |
| 3 | [`archive_acl_clear`](archive_acl_clear.md) | `archive_acl.c` | `archive_acl_clear.pointer_dereference.81` | `confirmed_system_entry` | realistic / medium | not_triggered |
| 4 | [`archive_acl_text_len`](archive_acl_text_len.md) | `archive_acl.c` | `archive_acl_text_len.overflow.12` | `confirmed_system_entry` | realistic / high | no_record |
| 5 | [`archive_acl_to_text_l`](archive_acl_to_text_l.md) | `archive_acl.c` | `archive_acl_text_len.overflow.12` | `confirmed_system_entry` | realistic / high | no_record |
| 6 | [`archive_acl_to_text_w`](archive_acl_to_text_w.md) | `archive_acl.c` | `archive_acl_to_text_w.pointer_dereference.77` | `confirmed_system_entry` | realistic / high | no_record |
| 7 | [`archive_be32enc`](archive_be32enc.md) | `archive_read_support_format_7zip.c` | `archive_be32enc.pointer_dereference.1` | `confirmed_system_entry` | realistic / medium | inconclusive |
| 8 | [`archive_le32enc`](archive_le32enc.md) | `archive_read_support_format_7zip.c` | `archive_le32enc.pointer_dereference.1` | `confirmed_dynamic` | realistic / high | confirmed |
| 9 | [`next_field`](next_field.md) | `archive_acl.c` | `next_field.pointer_arithmetic.11` | `confirmed_bmc` | realistic / high | no_record |
| 10 | [`next_field_w`](next_field_w.md) | `archive_acl.c` | `next_field_w.pointer_arithmetic.11` | `confirmed_system_entry` | realistic / high | no_record |

## Evidence breakdown

How strong is the runtime evidence behind each finding?

| Dynamic outcome | Count | What it means |
|---|---|---|
| `confirmed` | 1 | GCC+ASAN harness actually crashed at runtime — PoC-grade evidence |
| `not_triggered` | 1 | Harness compiled and ran clean; the specific CBMC witness didn't reproduce. Bug may still be real with different inputs. |
| `inconclusive` | 1 | Harness compile failed (e.g. private headers); no runtime signal either way. |
| `no_record` / `skipped` | 7 | Dynamic didn't run (disabled, or both static checks errored). |

## Per-finding scenarios

**1. [`append_entry`](append_entry.md)** — `archive_acl.c`

> An attacker creates a malicious archive file with crafted POSIX.1e ACL entries. By controlling the ACL text format parsed by archive_acl_from_text_l (line 4370-4587), they can inject entries with a ma

**2. [`append_entry_w`](append_entry_w.md)** — `archive_acl.c`

> An attacker creates a malicious archive containing NFSv4 ACL entries with specific combinations of permission bits (setting all 14 permission flags) and flag bits (setting all 7 inheritance flags) wit

**3. [`archive_acl_clear`](archive_acl_clear.md)** — `archive_acl.c`

> An attacker crafts a malicious archive file that triggers a specific sequence of ACL parsing operations. During processing, the library allocates `acl_text_w` through `archive_acl_to_text_w`, then enc

**4. [`archive_acl_text_len`](archive_acl_text_len.md)** — `archive_acl.c`

> An attacker crafts a malicious archive file (e.g., tar, cpio, zip) containing ACL entries with extremely long username/group name strings or an enormous number of ACL entries. When libarchive parses t

**5. [`archive_acl_to_text_l`](archive_acl_to_text_l.md)** — `archive_acl.c`

> An attacker creates a malicious archive file containing an entry with a crafted ACL that, when parsed by libarchive's archive_acl_from_text_l or similar functions, results in thousands of ACL entries

**6. [`archive_acl_to_text_w`](archive_acl_to_text_w.md)** — `archive_acl.c`

> An attacker creates a malicious archive file (tar, zip, etc.) containing crafted ACL metadata. When libarchive parses this archive and calls `archive_acl_to_text_w()` to convert the ACL to text format

**7. [`archive_be32enc`](archive_be32enc.md)** — `archive_read_support_format_7zip.c`

> An attacker crafts a malformed 7-zip archive with manipulated metadata that causes the archive reader to attempt encoding data into an uninitialized or failed-allocation buffer pointer. When the reade

**8. [`archive_le32enc`](archive_le32enc.md)** — `archive_read_support_format_7zip.c`

> An attacker crafts a malformed 7-Zip archive that causes the libarchive parser to attempt encoding metadata (e.g., timestamps, file sizes) into a buffer whose pointer has been corrupted or improperly

**9. [`next_field`](next_field.md)** — `archive_acl.c`

> An attacker crafts a malicious archive file with an ACL text field containing carefully placed separator characters (tabs, newlines, colons, commas) such that the cumulative pointer advances in next_f

**10. [`next_field_w`](next_field_w.md)** — `archive_acl.c`

> An attacker crafts a malicious archive file (tar, zip, etc.) with an ACL entry containing a wide-character string like 'user:X:\b\b:rwx' where \b represents backspace characters (Unicode 0x08). When l

## How to read these findings

1. The **realism check** is the audit. bmc-agent-sec counts a finding as a real bug when the LLM auditor (given full code context, callers, dynamic outcome) votes REALISTIC. The same LLM that finds bugs is told to be its own skeptic.
2. The **dynamic outcome** column tells you whether the GCC+ASAN runtime check independently confirmed the crash. `confirmed` is the strongest evidence; `not_triggered` is the most ambiguous (could be a real bug with a different attacker input, could be an FP).
3. The **tier** column is the pipeline's classification: `confirmed_dynamic` > `confirmed_system_entry` > `confirmed_bmc`. The tier guard ensures `confirmed_dynamic` is only assigned when dynamic actually crashed.
4. Open the per-finding `<function>.md` for: full realism reasoning, attacker scenario, CBMC counterexample witness, function source, and concrete `cbmc` reproduction command.
5. The exact harness CBMC verified is committed as `<function>.harness.c` alongside each report.

## Caveats every reviewer should know

- **Realism nondeterminism**: the LLM can flip on the same CEx across runs (~10% on borderline cases). We use the strongest realistic CEx per function; downgraded CExes for the same function are preserved in `bug_reports/<property>.json` in the sweep artifact tree.
- **Pre-classifier disabled by default**: an earlier static filter was killing seed bugs before realism could see them. It's off now.
- **Realism endorses ≠ verified**: realism's reasoning is plausible but the LLM may hypothesize an upstream condition that isn't actually reachable. Manual code audit or successful PoC reproduction is the gold standard before reporting upstream.
- **Reports auto-generated** by `bmc_agent/report_generator.py`; reviewers should treat them as primary-source audit-trail dumps, not as polished disclosures.

