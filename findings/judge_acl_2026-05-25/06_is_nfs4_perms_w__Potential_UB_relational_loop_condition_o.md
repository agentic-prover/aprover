# Bug #6: is_nfs4_perms_w — Potential UB: relational loop condition on possibly NULL pointers

**LLM confidence (hypothesis):** medium
**BMC re-confirmation verdict:** realistic (confirmed=True)
**Originating judge:** isint_w / isint_w.pointer.1
**Seed-bug match heuristic:** Related to 4b3ba035 family (sibling helper that receives the same potentially-NULL field pointers; the upstream commit only patched ONE call site)

## What the LLM thinks the bug is

**Location:** /tmp/libarchive_seedhunt_full/archive_acl.c:1490-1589 (is_nfs4_perms_w / is_nfs4_flags_w)

**Bug type:** Potential UB: relational loop condition on possibly NULL pointers

**Attacker scenario (public-API trigger):**

> If NFSv4 ACL entries are missing required fields, archive_acl_from_text_w may call these with start==end==NULL; the while (p < end) comparison on invalid pointers is undefined.

## BMC re-verification

After GPT-5 emitted this adjacent-bug hypothesis from the `isint_w` primary judge, the pipeline generated a fresh CBMC harness focused on `is_nfs4_perms_w` (unwind=16) and re-ran CBMC. CBMC produced this failing property:

  `is_nfs4_perms_w.pointer.1`

A second LLM call (with no memory of the first) judged this new CEx with the hypothesis as context. Its reasoning:

> The failing check is a same-object pointer comparison in is_nfs4_perms_w at archive_acl.c:1495 (the while (p < end) loop). The real in-corpus caller archive_acl_from_text_w (called by the public API archive_entry_acl_from_text_w) invokes is_nfs4_perms_w with field[1+n].start/end that are set to NULL when the NFSv4 ACL entry is missing fields. There is no caller-side null check before the call, so start==end==NULL reaches is_nfs4_perms_w and triggers undefined behavior on the relational pointer comparison. CBMC’s witness uses two distinct local objects, but the same-object violation is also reachable in production via NULL/NULL from malformed input, making this a real bug.

**Attacker scenario (from BMC-confirmation judge):**

> An attacker provides a malformed NFSv4 ACL text (e.g., via an application calling archive_entry_acl_from_text_w with untrusted input, or via formats that embed ACL text) that omits the permission field. In archive_acl_from_text_w, field[1+n].start and .end become NULL, and the code unconditionally calls is_nfs4_perms_w(field[1+n].start, field[1+n].end, &permset). Inside is_nfs4_perms_w, the while (p < end) comparison at archive_acl.c:1495 performs a relational comparison on NULL pointers, invoking undefined behavior that can lead to a crash or misparse.

## Target function source (file: archive_acl.c)

```c
 1490  static int
 1491  is_nfs4_perms_w(const wchar_t *start, const wchar_t *end, int *permset)
 1492  {
 1493  	const wchar_t *p = start;
 1494  
 1495  	while (p < end) {
 1496  		switch (*p++) {
 1497  		case L'r':
 1498  			*permset |= ARCHIVE_ENTRY_ACL_READ_DATA;
 1499  			break;
 1500  		case L'w':
 1501  			*permset |= ARCHIVE_ENTRY_ACL_WRITE_DATA;
 1502  			break;
 1503  		case L'x':
 1504  			*permset |= ARCHIVE_ENTRY_ACL_EXECUTE;
 1505  			break;
 1506  		case L'p':
 1507  			*permset |= ARCHIVE_ENTRY_ACL_APPEND_DATA;
 1508  			break;
 1509  		case L'D':
 1510  			*permset |= ARCHIVE_ENTRY_ACL_DELETE_CHILD;
 1511  			break;
 1512  		case L'd':
 1513  			*permset |= ARCHIVE_ENTRY_ACL_DELETE;
 1514  			break;
 1515  		case L'a':
 1516  			*permset |= ARCHIVE_ENTRY_ACL_READ_ATTRIBUTES;
 1517  			break;
 1518  		case L'A':
 1519  			*permset |= ARCHIVE_ENTRY_ACL_WRITE_ATTRIBUTES;
 1520  			break;
 1521  		case L'R':
 1522  			*permset |= ARCHIVE_ENTRY_ACL_READ_NAMED_ATTRS;
 1523  			break;
 1524  		case L'W':
 1525  			*permset |= ARCHIVE_ENTRY_ACL_WRITE_NAMED_ATTRS;
 1526  			break;
 1527  		case L'c':
 1528  			*permset |= ARCHIVE_ENTRY_ACL_READ_ACL;
 1529  			break;
 1530  		case L'C':
 1531  			*permset |= ARCHIVE_ENTRY_ACL_WRITE_ACL;
 1532  			break;
 1533  		case L'o':
 1534  			*permset |= ARCHIVE_ENTRY_ACL_WRITE_OWNER;
 1535  			break;
 1536  		case L's':
 1537  			*permset |= ARCHIVE_ENTRY_ACL_SYNCHRONIZE;
 1538  			break;
 1539  		case L'-':
 1540  			break;
 1541  		default:
 1542  			return(0);
 1543  		}
 1544  	}
 1545  	return (1);
 1546  }
```

## Originating judge (isint_w)

The primary CBMC counterexample that led GPT-5 to surface this hypothesis was on `isint_w` / `isint_w.pointer.1`. GPT-5 voted UNREALISTIC on that primary CEx with the reasoning:

> The failure is CBMC’s “same object” check on the relational pointer comparison start >= end in isint_w. The harness supplies start and end as addresses of two unrelated stack scalars, so comparing them is undefined and triggers the check. In real code, the only in-corpus caller is archive_acl_from_text_w, which passes start/end that are two pointers into the same wide string sliced by next_field_w, making the comparison well-defined. Therefore, the counterexample relies on an impossible caller input in this corpus rather than a real bug. The violation occurs at the start >= end check in isint_w (archive_acl.c around line 1433).

## Manual verification checklist

- [ ] Read `is_nfs4_perms_w` in `archive_acl.c` and confirm the cited line / condition matches the LLM's claim.
- [ ] Trace from the public API entry to `is_nfs4_perms_w` and check whether ANY code path leaves the cited input in the unsafe state (NULL pointer, length=0, etc.) the LLM claims.
- [ ] Check upstream libarchive history for an already-landed fix near `is_nfs4_perms_w` matching this pattern (`git log -p libarchive/archive_acl.c | grep -A20 "is_nfs4_perms_w"`).
- [ ] If not patched: construct a minimal PAX tar / ACL input matching the attacker scenario; build with AddressSanitizer + UBSan; verify the crash.
- [ ] If reproducible: file as a defensive-coding gap upstream (no CVE class unless trivially exploitable).
