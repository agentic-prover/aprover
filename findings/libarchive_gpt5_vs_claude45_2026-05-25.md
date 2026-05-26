# GPT-5 vs Claude-Sonnet-4.5 head-to-head — 2026-05-25

Same 7-file libarchive corpus, same `verify-dir --lite-mode --enable-realism-check --threat-model security` flags, same `BMC_AGENT_DEDUP_MAX_PER_TYPE=3`. Only the LLM backend differs.

| Metric | GPT-5 | Claude 4.5 |
|---|---:|---:|
| Total bug_reports | 38 | 387 |
| Confidence != unlikely | 0 | 43 |
| Realism verdict = realistic | 0 | 0 |
| Documented seed-commits matched | 0 | 2 |

## Documented seed-commit coverage

- GPT-5 commits: []
- Claude 4.5 commits: ['1f2da75f', '8308b61c']
- Only GPT-5: []
- Only Claude 4.5: ['1f2da75f', '8308b61c']
- Both: []

## Only GPT-5 confirmed (Claude downgraded or absent) (0)
_none_

## Only Claude 4.5 confirmed (GPT-5 downgraded or absent) (43)
| File | Function | Confidence | Verdict | Property |
|---|---|---|---|---|
| archive_acl | `next_field` ★ | confirmed_system_entry | uncertain | `next_field.pointer_dereference.59` |
| archive_read_support_format_cab | `archive_be16dec` | confirmed_system_entry | uncertain | `archive_be16dec.pointer_dereference.1` |
| archive_read_support_format_cab | `archive_be16enc` | confirmed_system_entry | uncertain | `archive_be16enc.pointer_dereference.1` |
| archive_read_support_format_cab | `archive_be64dec` | confirmed_system_entry | uncertain | `archive_be32dec.pointer_dereference.1` |
| archive_read_support_format_cab | `archive_be64enc` | confirmed_system_entry | uncertain | `archive_be32enc.pointer_dereference.1` |
| archive_read_support_format_cab | `archive_le64enc` | confirmed_system_entry | uncertain | `archive_le32enc.pointer_dereference.1` |
| archive_read_support_format_cab | `archive_read_format_cab_bid` | confirmed_system_entry | unrealistic | `archive_read_format_cab_bid.pointer_arithmetic.5` |
| archive_read_support_format_cab | `archive_read_format_cab_cleanup` | confirmed_system_entry | unrealistic | `archive_read_format_cab_cleanup.precondition_instance.1` |
| archive_read_support_format_cab | `archive_read_format_cab_options` | confirmed_system_entry | unrealistic | `archive_read_format_cab_options.pointer_dereference.7` |
| archive_read_support_format_cab | `archive_read_format_cab_read_data` | confirmed_system_entry | unrealistic | `archive_read_format_cab_read_data.pointer_dereference.7` |
| archive_read_support_format_cab | `archive_read_format_cab_read_data_skip` | confirmed_system_entry | unrealistic | `archive_read_format_cab_read_data_skip.pointer_dereference.7` |
| archive_read_support_format_cab | `archive_read_format_cab_read_header` | confirmed_system_entry | unrealistic | `archive_read_format_cab_read_header.pointer_dereference.7` |
| archive_read_support_format_cab | `lzx_cleanup_bitstream` | confirmed_system_entry | uncertain | `lzx_cleanup_bitstream.pointer_dereference.8` |
| archive_read_support_format_cpio | `archive_read_format_cpio_bid` | confirmed_system_entry | unrealistic | `memcmp.pointer_dereference.5` |
| archive_read_support_format_cpio | `archive_read_format_cpio_cleanup` | confirmed_system_entry | unrealistic | `archive_read_format_cpio_cleanup.precondition_instance.1` |
| archive_read_support_format_cpio | `archive_read_format_cpio_options` | confirmed_system_entry | unrealistic | `archive_read_format_cpio_options.pointer_dereference.7` |
| archive_read_support_format_cpio | `archive_read_format_cpio_read_data` | confirmed_system_entry | uncertain | `archive_read_format_cpio_read_data.pointer_dereference.7` |
| archive_read_support_format_cpio | `archive_read_format_cpio_read_header` ★ | confirmed_system_entry | unrealistic | `archive_read_format_cpio_read_header.pointer_dereference.7` |
| archive_read_support_format_cpio | `archive_read_format_cpio_skip` | confirmed_system_entry | uncertain | `archive_read_format_cpio_skip.pointer_dereference.7` |
| archive_read_support_format_cpio | `header_bin_be` | confirmed_system_entry | unrealistic | `be4.pointer_dereference.5` |
| archive_read_support_format_cpio | `header_bin_le` | confirmed_system_entry | unrealistic | `header_bin_le.pointer_dereference.17` |
| archive_read_support_format_cpio | `header_newc` | confirmed_system_entry | unrealistic | `memcmp.pointer_dereference.5` |
| archive_read_support_format_iso9660 | `archive_be16dec` | confirmed_system_entry | uncertain | `archive_be16dec.pointer_dereference.1` |
| archive_read_support_format_iso9660 | `archive_be16enc` | confirmed_system_entry | uncertain | `archive_be16enc.pointer_dereference.1` |
| archive_read_support_format_iso9660 | `archive_be64dec` | confirmed_system_entry | uncertain | `archive_be32dec.pointer_dereference.1` |
| archive_read_support_format_iso9660 | `archive_be64enc` | confirmed_system_entry | uncertain | `archive_be32enc.pointer_dereference.1` |
| archive_read_support_format_iso9660 | `archive_le16enc` | confirmed_bmc | uncertain | `archive_le16enc.pointer_dereference.1` |
| archive_read_support_format_iso9660 | `archive_le64dec` | confirmed_bmc | uncertain | `archive_le32dec.pointer_dereference.1` |
| archive_read_support_format_iso9660 | `archive_le64enc` | confirmed_system_entry | uncertain | `archive_le32enc.pointer_dereference.1` |
| archive_read_support_format_iso9660 | `archive_read_format_iso9660_bid` | confirmed_system_entry | unrealistic | `memcmp.pointer_dereference.5` |
| archive_read_support_format_iso9660 | `archive_read_format_iso9660_cleanup` | confirmed_system_entry | unrealistic | `archive_read_format_iso9660_cleanup.precondition_instance.1` |
| archive_read_support_format_iso9660 | `archive_read_format_iso9660_options` | confirmed_system_entry | unrealistic | `archive_read_format_iso9660_options.pointer_dereference.7` |
| archive_read_support_format_iso9660 | `archive_read_format_iso9660_read_data` | confirmed_system_entry | unrealistic | `archive_read_format_iso9660_read_data.pointer_dereference.7` |
| archive_read_support_format_iso9660 | `archive_read_format_iso9660_read_header` | confirmed_system_entry | unrealistic | `strlen.pointer_dereference.1` |
| archive_read_support_format_rar5 | `archive_be16dec` | confirmed_system_entry | uncertain | `archive_be16dec.pointer_dereference.1` |
| archive_read_support_format_rar5 | `archive_be16enc` | confirmed_system_entry | uncertain | `archive_be16enc.pointer_dereference.1` |
| archive_read_support_format_rar5 | `archive_be64dec` | confirmed_system_entry | uncertain | `archive_be32dec.pointer_dereference.1` |
| archive_read_support_format_rar5 | `archive_be64enc` | confirmed_system_entry | uncertain | `archive_be32enc.pointer_dereference.1` |
| archive_read_support_format_rar5 | `archive_le16enc` | confirmed_bmc | uncertain | `archive_le16enc.pointer_dereference.1` |
| archive_read_support_format_rar5 | `archive_le64enc` | confirmed_system_entry | uncertain | `archive_le32enc.pointer_dereference.1` |
| archive_read_support_format_rar5 | `rar5_bid` | confirmed_system_entry | unrealistic | `memcmp.pointer_dereference.7` |
| archive_read_support_format_rar5 | `rar5_has_encrypted_entries` | confirmed_system_entry | unrealistic | `rar5_has_encrypted_entries.pointer_dereference.13` |
| archive_read_support_format_rar5 | `rar5_read_header` | confirmed_system_entry | unrealistic | `get_context.pointer_dereference.7` |

## Both confirmed (0)

(★ = function matches a documented seed-bug commit)
