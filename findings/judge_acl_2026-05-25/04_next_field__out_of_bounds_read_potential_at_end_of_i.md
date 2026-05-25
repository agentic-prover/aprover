# Bug #4: next_field — out-of-bounds read potential at end-of-input (dereferencing **p when length==0)

**LLM confidence (hypothesis):** medium
**BMC re-confirmation verdict:** realistic (confirmed=True)
**Originating judge:** is_nfs4_perms / is_nfs4_perms.pointer.1
**Seed-bug match heuristic:** MATCH documented seed 8308b61c (ACL parser OOB read)

## What the LLM thinks the bug is

**Location:** archive_acl.c:2102-2139 (next_field)

**Bug type:** out-of-bounds read potential at end-of-input (dereferencing **p when length==0)

**Attacker scenario (public-API trigger):**

> Feed archive_entry_acl_from_text (-> archive_acl_from_text_nl) an ACL string that ends without a trailing separator (no final comma/newline) and with provided length exactly at buffer end, so that next_field consumes the last field and reduces *l to 0, then sets *sep = **p, reading one byte past the provided buffer.

**LLM-cited evidence (from initial adjacent-bug hunt):**

> next_field sets *sep = **p unconditionally after loops (archive_acl.c:2124), but the loops only guard on *l > 0; when *l becomes 0 at buffer end without encountering a separator, **p dereferences a one-past-end pointer. Outer caller uses explicit length (archive_acl_from_text_nl signature includes size_t length), so text need not be NUL-terminated to make this safe.

## BMC re-verification

After GPT-5 emitted this adjacent-bug hypothesis from the `is_nfs4_perms` primary judge, the pipeline generated a fresh CBMC harness focused on `next_field` (unwind=16) and re-ran CBMC. CBMC produced this failing property:

  `next_field.pointer_dereference.83`

A second LLM call (with no memory of the first) judged this new CEx with the hypothesis as context. Its reasoning:

> The failing dereference is in next_field at line where it does *sep = **p without checking *l. In archive_acl.c:2102–2140, next_field advances *p and decrements *l while scanning. If the input ends without a trailing separator (comma/colon/newline) and the provided length exactly matches the remaining bytes, the loops consume the entire field so *l becomes 0 and *p points one past the end. The code then does *sep = **p (and may repeat for the comment-skip path), which dereferences one byte beyond the caller-provided buffer. This function is called by archive_acl_from_text_nl (archive_acl.c:1699) as part of parsing ACL strings, which is reachable via public APIs (archive_entry_acl_from_text -> archive_acl_from_text_l -> archive_acl_from_text_nl). An attacker providing an ACL string without a trailing separator and with length set to the exact buffer size can trigger the OOB read at next_field’s *sep = **p.

**Attacker scenario (from BMC-confirmation judge):**

> A user supplies a crafted ACL text string to archive_entry_acl_from_text (public API), with no trailing comma/newline and length equal to the buffer size (e.g., via archive formats or application input). The parser calls archive_acl_from_text_l -> archive_acl_from_text_nl, which invokes next_field(&text, &length, ...) to parse fields. When the last field exactly ends at the buffer end, next_field reduces length to 0 and leaves p at one-past-end, then performs *sep = **p, reading one byte past the input buffer (and potentially again in the comment-skip branch), causing an out-of-bounds read.

## Target function source (file: archive_acl.c)

```c
 2101  static void
 2102  next_field(const char **p, size_t *l, const char **start,
 2103      const char **end, char *sep)
 2104  {
 2105  	/* Skip leading whitespace to find start of field. */
 2106  	while (*l > 0 && (**p == ' ' || **p == '\t' || **p == '\n')) {
 2107  		(*p)++;
 2108  		(*l)--;
 2109  	}
 2110  	*start = *p;
 2111  
 2112  	/* Locate end of field, trim trailing whitespace if necessary */
 2113  	while (*l > 0 && **p != ' ' && **p != '\t' && **p != '\n' && **p != ',' && **p != ':' && **p != '#') {
 2114  		(*p)++;
 2115  		(*l)--;
 2116  	}
 2117  	*end = *p;
 2118  
 2119  	/* Scan for the separator. */
 2120  	while (*l > 0 && **p != ',' && **p != ':' && **p != '\n' && **p != '#') {
 2121  		(*p)++;
 2122  		(*l)--;
 2123  	}
 2124  	*sep = **p;
 2125  
 2126  	/* Handle in-field comments */
 2127  	if (*sep == '#') {
 2128  		while (*l > 0 && **p != ',' && **p != '\n') {
 2129  			(*p)++;
 2130  			(*l)--;
 2131  		}
 2132  		*sep = **p;
 2133  	}
 2134  
 2135  	/* Skip separator. */
 2136  	if (*l > 0) {
 2137  		(*p)++;
 2138  		(*l)--;
 2139  	}
 2140  }
```

## Originating judge (is_nfs4_perms)

The primary CBMC counterexample that led GPT-5 to surface this hypothesis was on `is_nfs4_perms` / `is_nfs4_perms.pointer.1`. GPT-5 voted UNREALISTIC on that primary CEx with the reasoning:

> The failure is a same-object pointer comparison violation at the while (p < end) check inside is_nfs4_perms. The CBMC harness supplies start and end as pointers into two different local arrays (_start_buf and _end_buf), so comparing p (derived from start) with end is undefined in C and triggers CBMC’s "same object" check. In real code, the only in-corpus caller, archive_acl_from_text_nl, obtains both start and end from next_field() while parsing a single text buffer; thus start and end always point into the same underlying object (the input text) and the comparison is well-defined. No real caller can reproduce the harness’s cross-object pointer comparison.

## Manual verification checklist

- [ ] Read `next_field` in `archive_acl.c` and confirm the cited line / condition matches the LLM's claim.
- [ ] Trace from the public API entry to `next_field` and check whether ANY code path leaves the cited input in the unsafe state (NULL pointer, length=0, etc.) the LLM claims.
- [ ] Check upstream libarchive history for an already-landed fix near `next_field` matching this pattern (`git log -p libarchive/archive_acl.c | grep -A20 "next_field"`).
- [ ] If not patched: construct a minimal PAX tar / ACL input matching the attacker scenario; build with AddressSanitizer + UBSan; verify the crash.
- [ ] If reproducible: file as a defensive-coding gap upstream (no CVE class unless trivially exploitable).
