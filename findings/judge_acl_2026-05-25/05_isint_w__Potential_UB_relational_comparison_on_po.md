# Bug #5: isint_w — Potential UB: relational comparison on possibly NULL pointers

**LLM confidence (hypothesis):** medium
**BMC re-confirmation verdict:** realistic (confirmed=True)
**Originating judge:** isint_w / isint_w.pointer.1
**Seed-bug match heuristic:** Related to 4b3ba035 family (sibling helper that receives the same potentially-NULL field pointers; the upstream commit only patched ONE call site)

## What the LLM thinks the bug is

**Location:** /tmp/libarchive_seedhunt_full/archive_acl.c:1429-1449 (isint_w)

**Bug type:** Potential UB: relational comparison on possibly NULL pointers

**Attacker scenario (public-API trigger):**

> Malformed ACL text with too few fields can cause archive_acl_from_text_w to pass field[k].start==field[k].end==NULL to isint_w; the start >= end comparison on NULL pointers is undefined by C.

## BMC re-verification

After GPT-5 emitted this adjacent-bug hypothesis from the `isint_w` primary judge, the pipeline generated a fresh CBMC harness focused on `isint_w` (unwind=16) and re-ran CBMC. CBMC produced this failing property:

  `isint_w.pointer.1`

A second LLM call (with no memory of the first) judged this new CEx with the hypothesis as context. Its reasoning:

> CBMC’s failure is at isint_w line comparing start >= end. The harness passed start/end as distinct stack scalars, triggering a same-object check, but this is also reachable via the real caller archive_acl_from_text_w. In archive_acl_from_text_w, when parsing an ACL entry with too few fields, it sets field[k].start = field[k].end = NULL for missing fields (lines ~1208–1214), then unconditionally calls isint_w(field[n+1].start, field[n+1].end, &id) (around lines ~1238–1246) without checking for NULL. That results in start==end==NULL being passed to isint_w, where the first operation performs a relational comparison start >= end, which is undefined for unrelated/NULL pointers and violates CBMC’s same-object property. This can be triggered by malformed ACL strings supplied through the public API archive_entry_acl_from_text_w → archive_acl_from_text_w.

**Attacker scenario (from BMC-confirmation judge):**

> An attacker supplies a malformed ACL text string to archive_entry_acl_from_text_w (e.g., via an archive containing ACL metadata parsed by libarchive) that omits the uid/gid field so that field[1] remains unset. archive_acl_from_text_w sets field[1].start = field[1].end = NULL and still calls isint_w(field[1].start, field[1].end, &id). isint_w then evaluates start >= end on two NULL pointers, invoking undefined behavior and violating memory model assumptions, potentially leading to miscompilation or faults.

## Target function source (file: archive_acl.c)

```c
 1429  static int
 1430  isint_w(const wchar_t *start, const wchar_t *end, int *result)
 1431  {
 1432  	int n = 0;
 1433  	if (start >= end)
 1434  		return (0);
 1435  	while (start < end) {
 1436  		if (*start < L'0' || *start > L'9')
 1437  			return (0);
 1438  		if (n > (INT_MAX / 10) ||
 1439  		    (n == INT_MAX / 10 && (*start - L'0') > INT_MAX % 10)) {
 1440  			n = INT_MAX;
 1441  		} else {
 1442  			n *= 10;
 1443  			n += *start - L'0';
 1444  		}
 1445  		start++;
 1446  	}
 1447  	*result = n;
 1448  	return (1);
 1449  }
```

## Originating judge (isint_w)

The primary CBMC counterexample that led GPT-5 to surface this hypothesis was on `isint_w` / `isint_w.pointer.1`. GPT-5 voted UNREALISTIC on that primary CEx with the reasoning:

> The failure is CBMC’s “same object” check on the relational pointer comparison start >= end in isint_w. The harness supplies start and end as addresses of two unrelated stack scalars, so comparing them is undefined and triggers the check. In real code, the only in-corpus caller is archive_acl_from_text_w, which passes start/end that are two pointers into the same wide string sliced by next_field_w, making the comparison well-defined. Therefore, the counterexample relies on an impossible caller input in this corpus rather than a real bug. The violation occurs at the start >= end check in isint_w (archive_acl.c around line 1433).

## Manual verification checklist

- [ ] Read `isint_w` in `archive_acl.c` and confirm the cited line / condition matches the LLM's claim.
- [ ] Trace from the public API entry to `isint_w` and check whether ANY code path leaves the cited input in the unsafe state (NULL pointer, length=0, etc.) the LLM claims.
- [ ] Check upstream libarchive history for an already-landed fix near `isint_w` matching this pattern (`git log -p libarchive/archive_acl.c | grep -A20 "isint_w"`).
- [ ] If not patched: construct a minimal PAX tar / ACL input matching the attacker scenario; build with AddressSanitizer + UBSan; verify the crash.
- [ ] If reproducible: file as a defensive-coding gap upstream (no CVE class unless trivially exploitable).
