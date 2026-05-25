# Bug #2: is_nfs4_flags — Undefined behavior on missing NFSv4 fields (NULL start/end)

**LLM confidence (hypothesis):** medium
**BMC re-confirmation verdict:** realistic (confirmed=True)
**Originating judge:** is_nfs4_flags / is_nfs4_flags.pointer.1
**Seed-bug match heuristic:** Related to 4b3ba035 family (sibling helper that receives the same potentially-NULL field pointers; the upstream commit only patched ONE call site)

## What the LLM thinks the bug is

**Location:** archive_acl.c:1881 (call) and 2056–2091 (is_nfs4_flags body)

**Bug type:** Undefined behavior on missing NFSv4 fields (NULL start/end)

**Attacker scenario (public-API trigger):**

> A malformed NFSv4 ACL entry with too few colon-separated fields can leave field[1+n] or field[2+n] as NULL in archive_acl_from_text_nl(). The code then calls is_nfs4_perms/flags with NULL start/end, leading to a relational comparison on invalid pointers in the while (p < end) loop. A crafted ACL string (e.g., "user:alice:rw" without flags/type/count) could trigger this and should be rejected before calling the parsers.

## BMC re-verification

After GPT-5 emitted this adjacent-bug hypothesis from the `is_nfs4_flags` primary judge, the pipeline generated a fresh CBMC harness focused on `is_nfs4_flags` (unwind=16) and re-ran CBMC. CBMC produced this failing property:

  `is_nfs4_flags.pointer.1`

A second LLM call (with no memory of the first) judged this new CEx with the hypothesis as context. Its reasoning:

> The CBMC failure on is_nfs4_flags arises because the harness passes unrelated buffers for start/end, violating the same-object requirement for p < end. That specific witness is an artifact. However, the real caller archive_acl_from_text_nl (archive_acl.c) can pass NULL for start/end when an NFSv4 ACL field is missing: after parsing, it sets any missing fields to NULL, then unconditionally calls is_nfs4_perms(field[1+n].start, field[1+n].end, ...) and is_nfs4_flags(field[2+n].start, field[2+n].end, ...). If a malformed ACL string has too few colon-separated fields, field[1+n] or field[2+n] will be NULL, leading to a relational comparison p < end on invalid pointers in is_nfs4_perms/flags (lines ~1997/2060), which is undefined behavior. This is reachable via the public ACL text parsing APIs that accept attacker-controlled text.

**Attacker scenario (from BMC-confirmation judge):**

> Provide a malformed NFSv4 ACL text to an API that invokes archive_acl_from_text_l/archive_acl_from_text_nl, for example via an mtree or pax attribute or any consumer that parses user-supplied ACL text. A string like "user:alice:rw" (missing flags and type fields) causes archive_acl_from_text_nl to set field[2] and beyond to NULL, then call is_nfs4_perms/flags with NULL start/end. The called functions perform while (p < end) and switch(*p++), invoking undefined behavior and potentially crashing or misbehaving.

## Target function source (file: archive_acl.c)

```c
 2055  static int
 2056  is_nfs4_flags(const char *start, const char *end, int *permset)
 2057  {
 2058  	const char *p = start;
 2059  
 2060  	while (p < end) {
 2061  		switch(*p++) {
 2062  		case 'f':
 2063  			*permset |= ARCHIVE_ENTRY_ACL_ENTRY_FILE_INHERIT;
 2064  			break;
 2065  		case 'd':
 2066  			*permset |= ARCHIVE_ENTRY_ACL_ENTRY_DIRECTORY_INHERIT;
 2067  			break;
 2068  		case 'i':
 2069  			*permset |= ARCHIVE_ENTRY_ACL_ENTRY_INHERIT_ONLY;
 2070  			break;
 2071  		case 'n':
 2072  			*permset |=
 2073  			    ARCHIVE_ENTRY_ACL_ENTRY_NO_PROPAGATE_INHERIT;
 2074  			break;
 2075  		case 'S':
 2076  			*permset |= ARCHIVE_ENTRY_ACL_ENTRY_SUCCESSFUL_ACCESS;
 2077  			break;
 2078  		case 'F':
 2079  			*permset |= ARCHIVE_ENTRY_ACL_ENTRY_FAILED_ACCESS;
 2080  			break;
 2081  		case 'I':
 2082  			*permset |= ARCHIVE_ENTRY_ACL_ENTRY_INHERITED;
 2083  			break;
 2084  		case '-':
 2085  			break;
 2086  		default:
 2087  			return (0);
 2088  		}
 2089  	}
 2090  	return (1);
 2091  }
```

## Originating judge (is_nfs4_flags)

The primary CBMC counterexample that led GPT-5 to surface this hypothesis was on `is_nfs4_flags` / `is_nfs4_flags.pointer.1`. GPT-5 voted UNREALISTIC on that primary CEx with the reasoning:

> The failing property is a same-object pointer comparison at archive_acl.c:2060 within is_nfs4_flags (the while (p < end) loop). The harness passes start and end as two unrelated buffers, so p and end are from different objects, which violates the function’s implicit precondition that end delimits a substring within the same buffer as start. In real code, the only in-corpus caller archive_acl_from_text_nl() (archive_acl.c:1881) obtains both pointers from next_field(), which always returns start/end within the same input buffer, so the p < end comparison is valid. Thus the counterexample is a harness artifact, not a reachable bug via public APIs.

## Manual verification checklist

- [ ] Read `is_nfs4_flags` in `archive_acl.c` and confirm the cited line / condition matches the LLM's claim.
- [ ] Trace from the public API entry to `is_nfs4_flags` and check whether ANY code path leaves the cited input in the unsafe state (NULL pointer, length=0, etc.) the LLM claims.
- [ ] Check upstream libarchive history for an already-landed fix near `is_nfs4_flags` matching this pattern (`git log -p libarchive/archive_acl.c | grep -A20 "is_nfs4_flags"`).
- [ ] If not patched: construct a minimal PAX tar / ACL input matching the attacker scenario; build with AddressSanitizer + UBSan; verify the crash.
- [ ] If reproducible: file as a defensive-coding gap upstream (no CVE class unless trivially exploitable).
