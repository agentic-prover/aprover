# Embargoed: libarchive archive_acl findings

5 bug-detail files originally landed on `main` (commits c260395, e83efb5, e3a0e02, 2a24c5b, 40be63f, 3bf0efc) have been MOVED to the private companion repo:

> https://github.com/agentic-prover/aprover-findings-embargoed

Reason: the files contain reproducers, source-line citations, and a draft patch for an UNFIXED security bug in libarchive (3.3.0 – 3.8.7). They are embargoed pending upstream notification.

Contents in the private repo (visible to repo collaborators):

- `findings/libarchive_archive_acl_to_text_heap_overflow_nfsv4_2026-05-27.md` — ASAN-confirmed heap-overflow + patch (Variant 1.A)
- `findings/libarchive_archive_acl_append_id_pointer_dereference_2026-05-27.md` — original CBMC CEx
- `findings/libarchive_archive_acl_append_id_w_pointer_arithmetic_2026-05-27.md` — wide-string analogue
- `findings/postfix8_archive_acl_triage_real_bugs_2026-05-27.md` — 16-verdict triage report (4 calc/writer variants + uninit-pointer + silent-drop)
- `findings/postfix8_archive_acl_triage_2026-05-27.md` — earlier 27-CEx triage v1 summary

KNOWN LIMITATION: the files remain in this public repo's git history at the commits listed above. A force-rewrite of `main` would be needed for true embargo; this pointer branch is the minimum-disruption record of the move.
