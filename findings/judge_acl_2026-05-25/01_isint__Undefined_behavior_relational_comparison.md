# Bug #1: isint — Undefined behavior: relational comparisons on possibly NULL or unrelated pointers

**LLM confidence (hypothesis):** medium
**BMC re-confirmation verdict:** realistic (confirmed=True)
**Originating judge:** archive_acl_from_text_nl / archive_acl_from_text_nl.unwind.0
**Seed-bug match heuristic:** Related to 4b3ba035 family (sibling helper that receives the same potentially-NULL field pointers; the upstream commit only patched ONE call site)

## What the LLM thinks the bug is

**Location:** archive_acl.c:1932-1951 (isint) and 1959-1985 (ismode) and 2056-2091 (is_nfs4_flags) and ~1987-2047 (is_nfs4_perms)

**Bug type:** Undefined behavior: relational comparisons on possibly NULL or unrelated pointers

**Attacker scenario (public-API trigger):**

> Call archive_entry_acl_from_text with malformed ACL lines that omit optional fields, causing field[i].start/end to be set to NULL for missing fields. The parser then calls isint/ismode/is_nfs4_* with these NULL start/end pairs. Those helpers perform pointer relational comparisons (start >= end, while (p < end)), which is undefined for NULL or unrelated pointers. This can lead to miscompilation or sanitizer crashes even if typical builds appear to work.

**LLM-cited evidence (from initial adjacent-bug hunt):**

> Helpers check start>=end or loop with p<end without guarding start/end against NULL. Missing fields are explicitly set start=end=NULL (archive_acl.c:1708-1713). C standard defines relational comparisons only for pointers into the same array; NULL is not permitted. See isint at 1934-1941, ismode at 1963-1971, is_nfs4_perms loop around 1987-2005, is_nfs4_flags at 2060-2068.

## BMC re-verification

After GPT-5 emitted this adjacent-bug hypothesis from the `archive_acl_from_text_nl` primary judge, the pipeline generated a fresh CBMC harness focused on `isint` (unwind=16) and re-ran CBMC. CBMC produced this failing property:

  `isint.pointer.1`

A second LLM call (with no memory of the first) judged this new CEx with the hypothesis as context. Its reasoning:

> Real bug. The only in-corpus caller archive_acl_from_text_nl builds start/end substrings with next_field(), but for missing/omitted fields it explicitly sets field[i].start = field[i].end = NULL (archive_acl.c:1707–1714). It then unconditionally calls isint(field[n+1].start, field[n+1].end, &id) (archive_acl.c:1749) before any NULL checks. isint performs a relational comparison on these pointers: if (start >= end) return 0; (archive_acl.c:1935–1937). Relational comparisons on NULL (or unrelated) pointers are undefined behavior and violate CBMC’s same-object check, matching the failing property. An attacker can pass malformed ACL text with too few colon-separated fields (e.g., "user") via the public API archive_acl_from_text_l(), triggering a call to isint with start=end=NULL and UB at that line.

**Attacker scenario (from BMC-confirmation judge):**

> Supply a malformed ACL string to archive_acl_from_text_l(), such as "user" or another entry with missing numeric/id fields. The parser sets field[1].start = field[1].end = NULL and still calls isint(field[1].start, field[1].end, &id). isint then evaluates if (start >= end), invoking undefined behavior on NULL pointer relational comparison, which can lead to sanitizer aborts or miscompilation.

## Target function source (file: archive_acl.c)

```c
 1931  static int
 1932  isint(const char *start, const char *end, int *result)
 1933  {
 1934  	int n = 0;
 1935  	if (start >= end)
 1936  		return (0);
 1937  	while (start < end) {
 1938  		if (*start < '0' || *start > '9')
 1939  			return (0);
 1940  		if (n > (INT_MAX / 10) ||
 1941  		    (n == INT_MAX / 10 && (*start - '0') > INT_MAX % 10)) {
 1942  			n = INT_MAX;
 1943  		} else {
 1944  			n *= 10;
 1945  			n += *start - '0';
 1946  		}
 1947  		start++;
 1948  	}
 1949  	*result = n;
 1950  	return (1);
 1951  }
```

## Originating judge (archive_acl_from_text_nl)

The primary CBMC counterexample that led GPT-5 to surface this hypothesis was on `archive_acl_from_text_nl` / `archive_acl_from_text_nl.unwind.0`. GPT-5 voted UNREALISTIC on that primary CEx with the reasoning:

> The failure is an unwinding assertion on the outer parsing loop in archive_acl_from_text_nl caused by the harness stubbing next_field with a no‑op. In the counterexample, next_field_stub does not advance text or decrement length or set sep, so the loop condition (while (text != NULL && length > 0 && *text != '\0')) never makes progress, triggering the unwinding failure. In the real code, next_field (archive_acl.c) updates the pointers/length and sets sep, guaranteeing progress and termination. The only in‑corpus caller archive_acl_from_text_l passes strlen(text) as length, so the loop is reachable with proper, terminating inputs. Therefore, the CBMC counterexample is a harness artifact, not a real bug.

## Manual verification checklist

- [ ] Read `isint` in `archive_acl.c` and confirm the cited line / condition matches the LLM's claim.
- [ ] Trace from the public API entry to `isint` and check whether ANY code path leaves the cited input in the unsafe state (NULL pointer, length=0, etc.) the LLM claims.
- [ ] Check upstream libarchive history for an already-landed fix near `isint` matching this pattern (`git log -p libarchive/archive_acl.c | grep -A20 "isint"`).
- [ ] If not patched: construct a minimal PAX tar / ACL input matching the attacker scenario; build with AddressSanitizer + UBSan; verify the crash.
- [ ] If reproducible: file as a defensive-coding gap upstream (no CVE class unless trivially exploitable).
