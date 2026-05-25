# Bug #3: archive_acl_from_text_nl — NULL pointer arithmetic/undefined behavior on truncated NFSv4 ACL fields

**LLM confidence (hypothesis):** high
**BMC re-confirmation verdict:** realistic (confirmed=True)
**Originating judge:** is_nfs4_flags / is_nfs4_flags.pointer.1
**Seed-bug match heuristic:** MATCH documented seed 4b3ba035 (NULL deref in archive_acl_from_text_nl)

## What the LLM thinks the bug is

**Location:** archive_acl.c:1875-1889 (archive_acl_from_text_nl NFSv4 path)

**Bug type:** NULL pointer arithmetic/undefined behavior on truncated NFSv4 ACL fields

**Attacker scenario (public-API trigger):**

> Call archive_entry_acl_from_text(…, "user:alice", ARCHIVE_ENTRY_ACL_TYPE_NFS4). next_field() produces only two fields; remaining required fields (perms, flags, type, id) are left NULL. The code then calls is_nfs4_perms(field[2].start, field[2].end, …) and is_nfs4_flags(field[3].start, field[3].end, …) with NULL/NULL, and subsequently computes len = field[3+n].end - field[3+n].start with both NULL, causing undefined behavior and potential crash.

**LLM-cited evidence (from initial adjacent-bug hunt):**

> File archive_acl.c shows fields beyond what was parsed are set to NULL (lines 1708-1709). In the NFSv4 branch, the code unconditionally calls is_nfs4_perms/flags with field[1+n]/field[2+n] (lines 1875-1883), and then does pointer subtraction on field[3+n] (lines 1887-1889) without NULL checks.

## BMC re-verification

After GPT-5 emitted this adjacent-bug hypothesis from the `is_nfs4_flags` primary judge, the pipeline generated a fresh CBMC harness focused on `archive_acl_from_text_nl` (unwind=16) and re-ran CBMC. CBMC produced this failing property:

  `archive_acl_from_text_nl.unwind.0`

A second LLM call (with no memory of the first) judged this new CEx with the hypothesis as context. Its reasoning:

> The CBMC failure is an unwinding-assertion caused by a no-op stub for next_field that never advances separators, but inspecting the real code reveals a genuine bug reachable from public callers. In the NFSv4 branch of archive_acl_from_text_nl (archive_acl.c:1875–1906), the code unconditionally computes len = field[3+n].end - field[3+n].start and uses field[3+n].start for memcmp without first ensuring those pointers are non-NULL. If the input provides too few colon-separated fields (e.g., only a tag and maybe a name), the parser sets remaining fields’ start/end to NULL (archive_acl.c:1708–1714), yet is_nfs4_perms and is_nfs4_flags accept empty fields (they return 1 when start==end and perform no checks), so the code proceeds to the len calculation on NULL pointers, yielding undefined behavior/crash. This is triggerable via archive_acl_from_text_l → archive_acl_from_text_nl with attacker-controlled text and want_type = ARCHIVE_ENTRY_ACL_TYPE_NFS4.

**Attacker scenario (from BMC-confirmation judge):**

> A program using libarchive calls archive_entry_acl_from_text(..., "user:alice", ARCHIVE_ENTRY_ACL_TYPE_NFS4). next_field parses only 1–2 fields; remaining fields are NULL. is_nfs4_perms(field[2]) and is_nfs4_flags(field[3]) return success on empty fields, then the code computes len = field[3].end - field[3].start and memcmp(s, ...) with s = field[3].start (both NULL), causing a crash or undefined behavior.

## Target function source (file: archive_acl.c)

```c
 1658  int
 1659  archive_acl_from_text_nl(struct archive_acl *acl, const char *text,
 1660      size_t length, int want_type, struct archive_string_conv *sc)
 1661  {
 1662  	struct {
 1663  		const char *start;
 1664  		const char *end;
 1665  	} field[6], name;
 1666  
 1667  	const char *s, *st;
 1668  	int numfields, fields, n, r, sol, ret;
 1669  	int type, types, tag, permset, id;
 1670  	size_t len;
 1671  	char sep;
 1672  
 1673  	switch (want_type) {
 1674  	case ARCHIVE_ENTRY_ACL_TYPE_POSIX1E:
 1675  		want_type = ARCHIVE_ENTRY_ACL_TYPE_ACCESS;
 1676  		__LA_FALLTHROUGH;
 1677  	case ARCHIVE_ENTRY_ACL_TYPE_ACCESS:
 1678  	case ARCHIVE_ENTRY_ACL_TYPE_DEFAULT:
 1679  		numfields = 5;
 1680  		break;
 1681  	case ARCHIVE_ENTRY_ACL_TYPE_NFS4:
 1682  		numfields = 6;
 1683  		break;
 1684  	default:
 1685  		return (ARCHIVE_FATAL);
 1686  	}
 1687  
 1688  	ret = ARCHIVE_OK;
 1689  	types = 0;
 1690  
 1691  	while (text != NULL && length > 0 && *text != '\0') {
 1692  		/*
 1693  		 * Parse the fields out of the next entry,
 1694  		 * advance 'text' to start of next entry.
 1695  		 */
 1696  		fields = 0;
 1697  		do {
 1698  			const char *start, *end;
 1699  			next_field(&text, &length, &start, &end, &sep);
 1700  			if (fields < numfields) {
 1701  				field[fields].start = start;
 1702  				field[fields].end = end;
 1703  			}
 1704  			++fields;
 1705  		} while (sep == ':');
 1706  
 1707  		/* Set remaining fields to blank. */
 1708  		for (n = fields; n < numfields; ++n)
 1709  			field[n].start = field[n].end = NULL;
 1710  
 1711  		if (field[0].start == NULL || field[0].end == NULL) {
 1712  			/* This should never happen */
 1713  			return (ARCHIVE_FATAL);
 1714  		}
 1715  
 1716  		if (*(field[0].start) == '#') {
 1717  			/* Comment, skip entry */
 1718  			continue;
 1719  		}
 1720  
 1721  		n = 0;
 1722  		sol = 0;
 1723  		id = -1;
 1724  		permset = 0;
 1725  		name.start = name.end = NULL;
 1726  
 1727  		if (want_type != ARCHIVE_ENTRY_ACL_TYPE_NFS4) {
 1728  			/* POSIX.1e ACLs */
 1729  			/*
 1730  			 * Default keyword "default:user::rwx"
 1731  			 * if found, we have one more field
 1732  			 *
 1733  			 * We also support old Solaris extension:
 1734  			 * "defaultuser::rwx" is the default ACL corresponding
 1735  			 * to "user::rwx", etc. valid only for first field
 1736  			 */
 1737  			s = field[0].start;
 1738  			len = field[0].end - field[0].start;
 1739  			if (*s == 'd' && (len == 1 || (len >= 7
 1740  			    && memcmp((s + 1), "efault", 6) == 0))) {
 1741  				type = ARCHIVE_ENTRY_ACL_TYPE_DEFAULT;
 1742  				if (len > 7)
 1743  					field[0].start += 7;
 1744  				else
 1745  					n = 1;
 1746  			} else
 1747  				type = want_type;
 1748  
 1749  			/* Check for a numeric ID in field n+1 or n+3. */
 1750  			isint(field[n + 1].start, field[n + 1].end, &id);
 1751  			/* Field n+3 is optional. */
 1752  			if (id == -1 && fields > (n + 3))
 1753  				isint(field[n + 3].start, field[n + 3].end,
 1754  				    &id);
 1755  
 1756  			tag = 0;
 1757  			s = field[n].start;
 1758  			st = field[n].start + 1;
 1759  			len = field[n].end - field[n].start;
 1760  
 1761  			if (len == 0) {
 1762  				ret = ARCHIVE_WARN;
 1763  				continue;
 1764  			}
 1765  
 1766  			switch (*s) {
 1767  			case 'u':
 1768  				if (len == 1 || (len == 4
 1769  				    && memcmp(st, "ser", 3) == 0))
 1770  					tag = ARCHIVE_ENTRY_ACL_USER_OBJ;
 1771  				break;
 1772  			case 'g':
 1773  				if (len == 1 || (len == 5
 1774  				    && memcmp(st, "roup", 4) == 0))
 1775  					tag = ARCHIVE_ENTRY_ACL_GROUP_OBJ;
 1776  				break;
 1777  			case 'o':
 1778  				if (len == 1 || (len == 5
 1779  				    && memcmp(st, "ther", 4) == 0))
 1780  					tag = ARCHIVE_ENTRY_ACL_OTHER;
 1781  				break;
 1782  			case 'm':
 1783  				if (len == 1 || (len == 4
 1784  				    && memcmp(st, "ask", 3) == 0))
 1785  					tag = ARCHIVE_ENTRY_ACL_MASK;
 1786  				break;
 1787  			default:
 1788  					break;
 1789  			}
 1790  
 1791  			switch (tag) {
 1792  			case ARCHIVE_ENTRY_ACL_OTHER:
 1793  			case ARCHIVE_ENTRY_ACL_MASK:
 1794  				if (fields == (n + 2)
 1795  				    && field[n + 1].start < field[n + 1].end
 1796  				    && ismode(field[n + 1].start,
 1797  				    field[n + 1].end, &permset)) {
 1798  					/* This is Solaris-style "other:rwx" */
 1799  					sol = 1;
 1800  				} else if (fields == (n + 3) &&
 1801  				    field[n + 1].start < field[n + 1].end) {
 1802  					/* Invalid mask or other field */
 1803  					ret = ARCHIVE_WARN;
 1804  					continue;
 1805  				}
 1806  				break;
 1807  			case ARCHIVE_ENTRY_ACL_USER_OBJ:
 1808  			case ARCHIVE_ENTRY_ACL_GROUP_OBJ:
 1809  				if (id != -1 ||
 1810  				    field[n + 1].start < field[n + 1].end) {
 1811  					name = field[n + 1];
 1812  					if (tag == ARCHIVE_ENTRY_ACL_USER_OBJ)
 1813  						tag = ARCHIVE_ENTRY_ACL_USER;
 1814  					else
 1815  						tag = ARCHIVE_ENTRY_ACL_GROUP;
 1816  				}
 1817  				break;
 1818  			default:
 1819  				/* Invalid tag, skip entry */
 1820  				ret = ARCHIVE_WARN;
 1821  				continue;
 1822  			}
 1823  
 1824  			/*
 1825  			 * Without "default:" we expect mode in field 3
 1826  			 * Exception: Solaris other and mask fields
 1827  			 */
 1828  			if (permset == 0 && !ismode(field[n + 2 - sol].start,
 1829  			    field[n + 2 - sol].end, &permset)) {
 1830  				/* Invalid mode, skip entry */
 1831  				ret = ARCHIVE_WARN;
 1832  				continue;
 1833  			}
 1834  		} else {
 1835  			/* NFS4 ACLs */
 1836  			s = field[0].start;
 1837  			len = field[0].end - field[0].start;
 1838  			tag = 0;
 1839  
 1840  			switch (len) {
 1841  			case 4:
 1842  				if (memcmp(s, "user", 4) == 0)
 1843  					tag = ARCHIVE_ENTRY_ACL_USER;
 1844  				break;
 1845  			case 5:
 1846  				if (memcmp(s, "group", 5) == 0)
 1847  					tag = ARCHIVE_ENTRY_ACL_GROUP;
 1848  				break;
 1849  			case 6:
 1850  				if (memcmp(s, "owner@", 6) == 0)
 1851  					tag = ARCHIVE_ENTRY_ACL_USER_
... [truncated; original 8288 chars]
```

## Originating judge (is_nfs4_flags)

The primary CBMC counterexample that led GPT-5 to surface this hypothesis was on `is_nfs4_flags` / `is_nfs4_flags.pointer.1`. GPT-5 voted UNREALISTIC on that primary CEx with the reasoning:

> The failing property is a same-object pointer comparison at archive_acl.c:2060 within is_nfs4_flags (the while (p < end) loop). The harness passes start and end as two unrelated buffers, so p and end are from different objects, which violates the function’s implicit precondition that end delimits a substring within the same buffer as start. In real code, the only in-corpus caller archive_acl_from_text_nl() (archive_acl.c:1881) obtains both pointers from next_field(), which always returns start/end within the same input buffer, so the p < end comparison is valid. Thus the counterexample is a harness artifact, not a reachable bug via public APIs.

## Manual verification checklist

- [ ] Read `archive_acl_from_text_nl` in `archive_acl.c` and confirm the cited line / condition matches the LLM's claim.
- [ ] Trace from the public API entry to `archive_acl_from_text_nl` and check whether ANY code path leaves the cited input in the unsafe state (NULL pointer, length=0, etc.) the LLM claims.
- [ ] Check upstream libarchive history for an already-landed fix near `archive_acl_from_text_nl` matching this pattern (`git log -p libarchive/archive_acl.c | grep -A20 "archive_acl_from_text_nl"`).
- [ ] If not patched: construct a minimal PAX tar / ACL input matching the attacker scenario; build with AddressSanitizer + UBSan; verify the crash.
- [ ] If reproducible: file as a defensive-coding gap upstream (no CVE class unless trivially exploitable).
