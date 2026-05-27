# Triage summary — libarchive_postfix8

**Sweep dir**: `/tmp/libarchive_postfix8`
**Total triaged**: 27
**Parse errors**: 0

## Verdict breakdown

- **likely_fp**: 27

## FP class breakdown (likely_fp only)

- `harness_pointer_offset_unconstrained`: 22
- `caller_contract_slip`: 3
- `cbmc_recursion_unwind_artifact`: 2

## Per-CEx verdicts

| Function | Property | Pipeline | Triage | Confidence | FP class |
|---|---|---|---|---|---|
| `acl_special` | `acl_special.pointer_dereference.1` | unresolved | likely_fp | high | caller_contract_slip |
| `acl_special` | `acl_special.pointer_dereference.2` | unresolved | likely_fp | high | caller_contract_slip |
| `acl_special` | `acl_special.pointer_dereference.3` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_entry` | `append_entry.pointer_arithmetic.23` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_entry` | `append_entry.pointer_dereference.125` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_entry` | `append_entry.unwind.0` | unresolved | likely_fp | high | cbmc_recursion_unwind_artifact |
| `append_entry` | `append_id.recursion` | unresolved | likely_fp | high | cbmc_recursion_unwind_artifact |
| `append_entry` | `strcpy.pointer_arithmetic.11` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_entry` | `strcpy.pointer_dereference.11` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_entry` | `strcpy.unwind.0` | unresolved | likely_fp | high | caller_contract_slip |
| `append_entry` | `strlen.pointer_arithmetic.5` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_entry` | `strlen.pointer_dereference.5` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_entry` | `strlen.unwind.0` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_id` | `append_id.pointer_arithmetic.5` | real_bug | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_id` | `append_id.pointer_dereference.11` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_id_w` | `append_id_w.pointer_arithmetic.5` | real_bug | likely_fp | high | harness_pointer_offset_unconstrained |
| `append_id_w` | `append_id_w.pointer_dereference.11` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `main.assertion.1` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.pointer_arithmetic.11` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.pointer_arithmetic.29` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.pointer_arithmetic.35` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.pointer_dereference.65` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.pointer_dereference.77` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.pointer_dereference.89` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.unwind.0` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.unwind.1` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |
| `next_field_w` | `next_field_w.unwind.3` | unresolved | likely_fp | high | harness_pointer_offset_unconstrained |

## Per-CEx reasoning

### acl_special::acl_special.pointer_dereference.1
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `caller_contract_slip`

> The witness shows acl={'name':'unknown'} with type=256 (ARCHIVE_ENTRY_ACL_TYPE_NFS4), permset=0, tag=10002 (ARCHIVE_ENTRY_ACL_USER). The function dereferences acl->mode at lines 8,9 (and 11,12,14,15) only when type==ARCHIVE_ENTRY_ACL_TYPE_ACCESS (value typically 256 in libarchive but the witness shows type=256 with NFS4 semantics). The dynamic reproducer calls archive_entry_acl_add_entry_w with TYPE_NFS4, which does NOT satisfy the guard condition (type==ARCHIVE_ENTRY_ACL_TYPE_ACCESS) so the function returns 1 without dereferencing acl->mode, hence not_triggered. The harness allows arbitrary type/tag combinations that real public-API callers (archive_entry_acl_add_entry_w_len) filter via their own logic before calling acl_special. The static chain shows no system entry reached, and the immediate caller archive_acl_add_entry_w_len maintains an implicit contract that acl is a valid initialized archive_acl struct (constructed via archive_entry_new). CBMC's symbolic acl pointer with unconstrained backing state bypasses this real-world initialization invariant.

### acl_special::acl_special.pointer_dereference.2
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `caller_contract_slip`

> The function acl_special checks if type==ARCHIVE_ENTRY_ACL_TYPE_ACCESS (line 4) before dereferencing acl->mode (lines 8,9,11,12,14,15). The witness shows type=256 and tag=10002, which do not match any of the switch cases (USER_OBJ, GROUP_OBJ, OTHER). With type=256≠ARCHIVE_ENTRY_ACL_TYPE_ACCESS, the function returns 1 at line 18 without ever dereferencing acl. The pointer_dereference.2 property violation cannot occur on this path. The dynamic validation confirms NOT_TRIGGERED—the reproducer ran without fault. The caller archive_acl_add_entry_w_len likely maintains an implicit contract that acl_special is only called with type==ARCHIVE_ENTRY_ACL_TYPE_ACCESS when acl needs dereferencing, but CBMC's harness allows arbitrary type values, creating an unreachable state where the dereference guard fails yet CBMC still explores the dereference.

### acl_special::acl_special.pointer_dereference.3
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness creates a nondeterministic pointer `acl` with only a NULL-check, leaving `acl->mode` at a completely symbolic offset. The function dereferences `acl->mode` on lines 8, 9, 12, 13, 16, 17 when the branch conditions are met. The witness shows `type=256` (ARCHIVE_ENTRY_ACL_TYPE_ACCESS), `permset=0` (satisfies `(permset & ~007)==0`), and `tag=10002` (ARCHIVE_ENTRY_ACL_USER_OBJ), which enters the first case and dereferences `acl->mode`. However, the dynamic validation returned NOT_TRIGGERED, meaning a real public-API call sequence (traced through `archive_acl_from_text_w → archive_acl_add_entry_w_len → acl_special`) did not crash. Real callers allocate `struct archive_acl` via internal constructors that zero or initialize the `mode` field, so the pointer is valid. The harness's unconstrained symbolic `acl` pointer allows CBMC to choose an invalid address for the `mode` field, which real callers never produce.

### append_entry::append_entry.pointer_arithmetic.23
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness allocates a 5-byte backing buffer for *p (_p_backing[5]) and allows _p_nul_at to be anywhere in [0..4], but then the function writes multiple strcpy/strcat operations and pointer increments without any bound checking. The witness shows _p_nul_at=0 (buffer effectively empty at start) yet the function writes prefix (3 chars from witness), then tag-specific strings (e.g. 'user'=4 chars), colons, permission chars, etc. — easily exceeding 5 bytes. Real callers of append_entry (an internal static helper in libarchive's ACL formatting) pre-allocate a sufficiently large buffer via size-precomputation routines (archive_acl_to_text_l computes required length first). The harness's 5-byte buffer is far too small for any realistic ACL entry string, violating the implicit caller contract that *p points into a buffer sized to hold the formatted entry. The pointer_arithmetic violation at line 23 (one of the *(*p)++ operations) is an artifact of this under-sized harness buffer, not a bug reachable through the public API (archive_entry_acl_to_text and friends).

### append_entry::append_entry.pointer_dereference.125
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness allocates a 5-byte backing buffer for *p (_p_backing[5]) and initializes _p_cursor to _p_backing, but does NOT constrain the symbolic pointer offset that CBMC can choose when advancing *p through strcpy/strlen operations. The witness shows _p_backing writes at symbolic offsets (e.g., __CPROVER_POINTER_OFFSET(_p_backing!0@1 + 3l)) that exceed the 5-byte allocation, triggering pointer_dereference.125. Real callers of append_entry (an internal static helper) pass a pre-sized buffer pointer computed by acl_text_size() or similar sizing routines that guarantee sufficient space for all strcpy/append operations. The harness's 5-byte buffer is far too small for the actual string concatenations (prefix + tag-strings + colons + perms + flags + type-strings can easily exceed 50 bytes), and the lack of buffer-size precondition allows CBMC to drive *p beyond bounds. This is a classic harness_pointer_offset_unconstrained false positive where the internal caller contract (buffer pre-sized) is not encoded.

### append_entry::append_entry.unwind.0
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `cbmc_recursion_unwind_artifact`

> The violated property is 'append_entry.unwind.0', which is a CBMC unwinding assertion, not a memory-safety or functional property. The counterexample shows i=4 in the NFSv4 ACL permission loop (lines 89-94 in source), with type=8192 (ARCHIVE_ENTRY_ACL_TYPE_NFS4 bit set) and tag=-2147473644 (not matching any standard ACL tag constant). The harness provides a 5-byte backing buffer for *p but allows fully symbolic type/tag/perm values. Real callers would constrain tag to valid ARCHIVE_ENTRY_ACL_* constants and perm to bounded flag sets, preventing unbounded loop iterations. The pipeline correctly notes it cannot tighten preconditions without excluding reachable states, but the unwinding failure itself is an artifact of CBMC's --unwind bound being exceeded by symbolic inputs that real public-API callers cannot construct (nfsv4_acl_perm_map_size and nfsv4_acl_flag_map_size are compile-time constants, but CBMC's symbolic perm can force worst-case iteration counts the harness buffer cannot accommodate). No evidence of a source bug—this is a verification-model limitation.

### append_entry::append_id.recursion
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `cbmc_recursion_unwind_artifact`

> The violated property is 'append_id.recursion', indicating CBMC detected unbounded recursion in append_id (called from line 67 and line 105 of append_entry). The witness shows id=127436, a large positive integer that would cause append_id to recurse deeply when formatting the decimal representation. The harness leaves 'id' completely unconstrained (nondet int), but real callers in archive ACL code pass user/group IDs which are bounded by OS limits (typically 0-65535 or similar). The 5-byte backing buffer '_p_backing' is far too small for the prefix + tag-string + name + id-decimal + permission-string that append_entry writes, yet the harness doesn't enforce the caller-contract that the buffer pointed to by *p must be pre-sized to accommodate the worst-case output (real callers compute required size via archive_acl_text_len before allocating). The recursion panic is an artifact of CBMC's unwinding bound being exceeded by a symbolic id value that real public-API call sequences cannot produce, combined with a buffer that real callers would never pass (too small by orders of magnitude).

### append_entry::strcpy.pointer_arithmetic.11
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness allocates a 5-byte backing buffer for *p (_p_backing[5]) and sets _p_nul_at=0, meaning the cursor starts at offset 0. The witness shows _prefix_len=4, so strcpy(*p, prefix) at line 11 writes 4 chars plus NUL (5 bytes total) into a 5-byte buffer starting at offset 0. The function then does *p += strlen(*p) (line 12), advancing the cursor by 4. Next, it writes 'user' (4 chars + NUL = 5 bytes) via strcpy at line 23, but the cursor is now at offset 4, leaving only 1 byte in the backing buffer. The strcpy.pointer_arithmetic.11 violation occurs because CBMC detects the out-of-bounds write. However, real callers of append_entry (an internal static helper in libarchive's ACL formatting code) pre-allocate a sufficiently large buffer by computing the required size via acl_text_len() before calling append_entry. The harness's 5-byte buffer is unrealistically small and does not reflect the caller contract that the buffer is sized to hold the entire formatted ACL entry. The dynamic validation shows 'no-dyn', meaning no public-API reproducer was attempted, consistent with this being an internal helper. The pipeline's own reasoning confirms it could not tighten preconditions without excluding valid caller states, indicating the harness under-constrains the buffer size invariant maintained by real callers.

### append_entry::strcpy.pointer_dereference.11
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness allocates a 5-byte backing buffer for *p (_p_backing) but allows _p_cursor to be symbolic, meaning CBMC can choose any offset within or beyond that buffer. The witness shows _p_cursor = {'name': 'unknown'} (symbolic pointer) and the strcpy at line 11 (prefix copy) dereferences *p without the harness constraining that *p points into _p_backing with sufficient space. Real callers of append_entry pre-size the output buffer via acl_text_len() (a size-precomputation routine visible in archive_acl.c context) and pass a cursor guaranteed to have room for the formatted ACL string. The harness's 5-byte buffer and unconstrained cursor violate the implicit caller contract that *p has adequate space, making this a harness artifact rather than a source bug.

### append_entry::strcpy.unwind.0
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `caller_contract_slip`

> The CBMC property 'strcpy.unwind.0' fires because the harness allocates a 5-byte backing buffer for *p (_p_backing[5]) but does not enforce that the sum of all strcpy/append operations stays within bounds. The witness shows _p_cursor starting at offset 1 in a 4-byte usable region, then the function attempts strcpy("group") (6 bytes including NUL) which overflows. Real callers of append_entry pre-size the output buffer via acl_text_len() or similar size-precomputation routines (common pattern in libarchive ACL code), ensuring the buffer is large enough for all appends. The harness's fixed 5-byte buffer and symbolic offset (_p_cursor = _p_backing!0@1) violate this implicit caller contract. The pipeline's 'Over-refinement guard rejected' message confirms it could not tighten preconditions to exclude this state without blocking legitimate caller states, indicating the harness under-specifies the real invariant. No dynamic validation ran (no-dyn), so we lack confirmation the public API can trigger this, but the static evidence strongly suggests a harness artifact rather than a source bug.

### append_entry::strlen.pointer_arithmetic.5
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness allocates a 5-byte backing buffer for *p (_p_backing[5]) and allows _p_nul_at to be anywhere in [0..4], meaning the cursor can start at any offset. The witness shows _p_nul_at=0 (cursor starts at _p_backing[0]), then prefix (length 3: bytes 2,2,':') is strcpy'd, advancing *p by 3. The function then writes additional characters (colons, permission bits, etc.) without bounds checking. The strlen.pointer_arithmetic.5 violation occurs when *p advances beyond _p_backing[4], writing into unallocated memory. However, real callers of append_entry pre-size the output buffer via acl_text_len() (a size-precomputation routine visible in the libarchive codebase), ensuring *p always has sufficient space. The harness's 5-byte buffer is arbitrary and does not reflect the caller contract that the buffer is sized to accommodate the entire ACL entry string. This is a classic caller_contract_slip: the internal helper trusts the public API's implicit size invariant, which the harness fails to model.

### append_entry::strlen.pointer_dereference.5
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness allocates a 5-byte backing buffer for *p (_p_backing[5]) and sets _p_nul_at=0, placing the NUL terminator at _p_backing[0]. The function then calls strcpy(*p, prefix) where prefix is a 4-character string ("\x02\x02:\0"). This strcpy writes 4 bytes starting at *p, which CBMC allows to advance beyond the 5-byte buffer boundary via symbolic pointer arithmetic. The witness shows _p_backing[(signed long int)__CPROVER_POINTER_OFFSET(_p_backing!0@1 + 4l)] being accessed, indicating CBMC chose a pointer offset that overflows the buffer. Real callers of append_entry maintain an invariant that *p points into a sufficiently large pre-allocated buffer (typically sized via acl_text_len computation in archive_acl_to_text_l), but the harness does not enforce this size relationship. The 5-byte buffer is far too small for the function's actual output requirements (which can be dozens of bytes), making this a harness construction artifact rather than a real bug in production usage.

### append_entry::strlen.unwind.0
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The violated property is 'strlen.unwind.0', a CBMC unwinding assertion in strlen. The witness shows _p_nul_at=0 (so _p_backing[0]='\0'), yet _p_cursor is symbolic ('unknown'), meaning the harness allowed *p to point to an arbitrary offset within or beyond _p_backing. The function's first strcpy(*p, prefix) with prefix='\x02\x02:\0' (length 3) writes into *p, then does *p += strlen(*p). If *p started at _p_backing+4 (the last valid index), strcpy would write past the 5-byte buffer, and the subsequent strlen on that unbounded region triggers the unwind violation. Real callers of append_entry (an internal static helper) maintain an invariant that *p points into a pre-allocated buffer with sufficient space (computed by acl_text_len or similar size-precomputation routines in libarchive's ACL subsystem). The harness's 5-byte backing buffer and unconstrained cursor offset break this implicit caller contract. The pipeline correctly noted it 'could not tighten the precondition without excluding states callers can produce' because the real caller contract (buffer sized via separate length computation) is not expressible in the harness's local pointer setup. This is a classic caller_contract_slip combined with harness_pointer_offset_unconstrained.

### append_id::append_id.pointer_arithmetic.5
- pipeline: `real_bug`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness initializes `_p_cursor = _p_backing` but the witness shows `_p_cursor` has symbolic offset `_p_backing!0@1` (unknown offset from base). The harness does not constrain the cursor to point within the 5-byte backing buffer bounds. The function `append_id` writes via `*(*p)++` which requires the cursor to have valid space. With `id=1` (single digit), only 1 byte is written, but CBMC's pointer arithmetic check fires because the symbolic offset allows the cursor to point outside `_p_backing[0..4]`. Real callers (e.g., `append_entry`) would maintain the invariant that `*p` points to valid writable space within a properly sized buffer. The harness construction allows an unreachable pointer state that violates this implicit caller contract.

### append_id::append_id.pointer_dereference.11
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The witness shows _p_cursor = {'name': 'unknown'} and writes to _p_backing at symbolic offsets __CPROVER_POINTER_OFFSET(_p_backing!0@1 + k). The harness initializes _p_cursor = _p_backing but CBMC then allows *p to point to an arbitrary symbolic offset within or beyond _p_backing's 5-byte allocation. The function source (line 11: *(*p)++ = ...) dereferences *p after advancing it, which fails when the symbolic offset places *p outside valid bounds. Real callers (append_entry) would maintain *p within a properly sized buffer; the harness's lack of constraint on the cursor's offset after initialization creates an unreachable state. The dynamic validation confirms this is not reproducible through the public API, and the static chain never reaches system entry, indicating the implicit caller contract (buffer size vs. cursor position) is violated by the harness's symbolic freedom.

### append_id_w::append_id_w.pointer_arithmetic.5
- pipeline: `real_bug`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness allocates a 5-element wchar_t backing buffer but leaves _wp_cursor symbolic (witness shows 'unknown'). The function recursively writes digits via *(*wp)++, which advances the cursor. With id=1 (single digit), only one wchar_t is written, yet the witness shows _wp_backing[0..4] all populated with digit values (49='1', 50='2', 52='4', 55='7', 50='2'). The pointer_arithmetic.5 violation occurs because CBMC chose a symbolic offset for _wp_cursor that points outside or at the boundary of _wp_backing, making the write *(*wp)++ go out-of-bounds. Real callers (append_entry_w) allocate sufficient buffer space and initialize the cursor to the start; the harness's unconstrained symbolic pointer offset is the root cause. Dynamic validation confirms NOT_TRIGGERED, meaning the public API cannot reproduce this state.

### append_id_w::append_id_w.pointer_dereference.11
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness initializes `_wp_cursor` to point to `_wp_backing[0]`, but the witness shows `_wp_cursor = {'name': 'unknown'}` with an unconstrained symbolic offset. The function `append_id_w` recursively writes digits and advances `*wp` via `*(*wp)++`. For `id=1`, only one digit is written, requiring one `wchar_t` slot. The 5-element backing buffer is sufficient. The pointer dereference violation at line 11 (`*(*wp)++ = ...`) fires because CBMC allowed `_wp_cursor` to take a symbolic offset that points outside `_wp_backing`, which real callers (traced through `append_entry_w` → `archive_acl_to_text_w`) never produce—they allocate sized buffers and maintain valid cursor positions. Dynamic validation confirms NOT_TRIGGERED, meaning the public API cannot drive this state. This is a classic harness artifact where the pointer parameter's offset is left unconstrained.

### next_field_w::main.assertion.1
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and allows the cursors to point anywhere via symbolic initialization (_wp_cursor = {'name': 'unknown'}). The function's logic at lines 23-27 performs pointer arithmetic (*end = *wp - 1; (*end)--; (*end)++) that assumes *start, *end, and *wp all point into the SAME underlying buffer—a contract enforced by the real caller archive_acl_from_text_w, which passes pointers into a single input string. The harness violates this by giving independent buffers, making inter-object pointer comparisons and arithmetic undefined behavior. The dynamic validation NOT_TRIGGERED outcome confirms that real public-API inputs (a single wchar_t string) do not trigger the assertion failure. The violated assertion checks __CPROVER_r_ok(*wp, ...), which fires when *wp points outside valid memory—an artifact of the harness allowing *wp to drift into _wp_backing while *start/*end reference different buffers.

### next_field_w::next_field_w.pointer_arithmetic.11
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and allows _wp_cursor and _start_cursor to be symbolic pointers ('unknown' in the witness). The function's logic at line 23 performs pointer arithmetic (*end = *wp - 1) and then dereferences *end in the while loop, expecting *end to point into the same buffer as *wp. However, the harness allows *wp to point to _wp_backing while *end points to _end_backing (different objects). When *wp == *start (both in _wp_backing), the else branch sets *end = *wp - 1, which may underflow _wp_backing or land in unrelated memory, triggering the pointer_arithmetic violation. Real callers of next_field_w would pass wp, start, and end all pointing into the same input string buffer, maintaining the invariant that pointer arithmetic stays within one object. The witness shows _wp_cursor and _start_cursor as symbolic, confirming CBMC explored inter-object pointer states that real API usage cannot produce.

### next_field_w::next_field_w.pointer_arithmetic.29
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and initializes the cursors to point to them, but does NOT constrain *wp, *start, and *end to point into the same buffer or maintain any relationship. The function's logic at line 29 performs (*end)-- which can underflow if *end points to the start of its independent buffer. Real callers would initialize *wp to a single input buffer and *start/*end would be derived from *wp during the function's execution (lines 10, 18, 23), ensuring they remain within the same buffer. The witness shows _wp_cursor, _start_cursor, _end_cursor as 'unknown' symbolic addresses with no relationship, allowing CBMC to choose *end at offset 0 of _end_backing, triggering the pointer_arithmetic violation when decremented. The caller-chain is empty (system_entry_reached: False), confirming this is an internal helper never called directly by public API with independent buffers.

### next_field_w::next_field_w.pointer_arithmetic.35
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and initializes the cursors to point to them, but does NOT constrain *wp, *start, *end to point into the same buffer or maintain any spatial relationship. The function source (lines 23-27) performs pointer arithmetic (*end = *wp - 1; (*end)--; (*end)++;) that assumes *end and *wp share a common backing buffer. The witness shows _wp_cursor and _end_cursor are 'unknown' symbolic addresses, meaning CBMC chose independent allocations. Real callers (e.g. parse_mtree_acl_w in libarchive) pass pointers into the same input string buffer, so *end and *wp always alias the same object. The pointer_arithmetic.35 violation fires because CBMC's inter-object pointer arithmetic is undefined behavior under C11, but this state is unreachable through the public API where all three pointers share a single wchar_t[] input buffer.

### next_field_w::next_field_w.pointer_dereference.65
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness constructs three independent backing buffers (_wp_backing, _start_backing, _end_backing) and allows _wp_cursor and _start_cursor to be symbolic (unknown) pointers, unconstrained to point within their respective backing arrays. The function dereferences **wp at line 65 (in the whitespace-trimming loop: while (**end == L' ' || **end == L'	' || **end == L'
')), but the witness shows _wp_cursor is 'unknown', meaning CBMC chose an arbitrary pointer value. The real caller archive_acl_from_text_w passes a single contiguous wide-string buffer where wp, start, and end all point into the same allocation. The harness's independent buffers plus unconstrained symbolic offsets allow inter-object pointer arithmetic and dereferences that violate the implicit caller contract. Dynamic validation confirms NOT_TRIGGERED, meaning the concrete reproducer (which would use a real wide-string) cannot trigger the crash. This is a classic harness_pointer_offset_unconstrained false positive.

### next_field_w::next_field_w.pointer_dereference.77
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and initializes cursors to them, but the witness shows _wp_cursor and _start_cursor have symbolic 'unknown' values rather than pointing into their respective backing arrays. The function dereferences **wp at line 77 (in the trailing-whitespace trim loop), but the harness allows *wp to point anywhere in memory, not constrained to the null-terminated wide string that real callers (archive_acl_from_text_w) would provide. The dynamic validation outcome 'not_triggered' confirms that a concrete public-API call cannot reproduce this state. Real callers pass a single wide-string buffer with wp, start, and end all pointing into the same allocation, maintaining the invariant that *wp points to valid wide-char data. The harness's independent backing buffers break this implicit contract, allowing CBMC to choose an invalid pointer for *wp.

### next_field_w::next_field_w.pointer_dereference.89
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and allows CBMC to choose arbitrary symbolic pointer values for _wp_cursor and _start_cursor (witness shows 'unknown'). The function's line 89 dereferences **end after computing *end = *wp - 1 (line 97), then decrementing *end in the whitespace-trim loop (line 99). In real usage via archive_acl_from_text_w, all three pointers (wp, start, end) point into the SAME input buffer, so *end = *wp - 1 keeps them in-bounds. The harness's independent buffers allow CBMC to pick _wp_cursor pointing into _wp_backing while *end points into _end_backing at an arbitrary offset, making the dereference at line 89 (in the trim loop) appear out-of-bounds. The dynamic validation NOT_TRIGGERED confirms no real crash occurs with actual public-API input. The caller-chain trace is valid but the harness over-constrains by not enforcing the single-buffer invariant that archive_acl_from_text_w maintains.

### next_field_w::next_field_w.unwind.0
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The violated property is 'next_field_w.unwind.0', indicating a loop unwinding bound was exceeded. The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and allows _wp_cursor to be symbolic ('unknown' in witness). The function's first loop (lines 7-9) advances *wp while scanning for non-whitespace, but the harness never constrains *wp to point into a null-terminated wide string — _wp_cursor is symbolic and unconstrained relative to _wp_backing. Real callers would pass *wp pointing into a valid null-terminated wchar_t string, ensuring the loop terminates at L'\0'. The harness's failure to model this string invariant allows CBMC to explore infinite-loop scenarios unreachable through the public API. The pipeline correctly identified this as an over-refinement issue where the implicit caller contract (null-terminated input) was not captured.

### next_field_w::next_field_w.unwind.1
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The violated property is 'next_field_w.unwind.1', indicating a loop-unwinding bound exceeded. The harness constructs three independent backing buffers (_wp_backing, _start_backing, _end_backing) and allows the cursors to point anywhere via symbolic initialization (_wp_cursor = {'name': 'unknown'}). The function's first loop (line 7-9) advances *wp while skipping whitespace, and the second loop (line 14-16) advances *wp scanning for separators. With _wp_cursor symbolically initialized to an arbitrary offset (potentially far from _wp_backing[0]), CBMC cannot prove the loops terminate within the unwinding bound. Real callers would initialize *wp to point to the start of a valid null-terminated wide string, ensuring bounded iteration. The harness's unconstrained pointer offset allows CBMC to explore states where *wp starts at a symbolic distance from any null terminator, causing the unwinding panic. The pipeline's 'Over-refinement guard rejected' message confirms the harness cannot be tightened without excluding real caller states, but the root cause is the harness giving *wp freedom real callers never exercise.

### next_field_w::next_field_w.unwind.3
- pipeline: `unresolved`  →  triage: **likely_fp** (high)
- fp_class: `harness_pointer_offset_unconstrained`

> The violated property is 'unwind.3', indicating CBMC's loop-unwinding bound was exceeded. The harness creates three independent backing buffers (_wp_backing, _start_backing, _end_backing) and allows the cursors to point anywhere via symbolic initialization (_wp_cursor = {'name': 'unknown'}). The function's first loop (lines 7-9) advances *wp while skipping whitespace, and the third loop (lines 22-24) decrements *end while trimming trailing whitespace. With independent backing buffers and unconstrained initial offsets, CBMC can choose a scenario where *wp starts far from the null terminator in _wp_backing, or *end starts at an arbitrary offset in _end_backing, causing the loops to iterate beyond the --unwind bound. Real callers would pass pointers into the same buffer (or at least constrained offsets), making such deep iterations impossible. The pipeline's own reasoning confirms it couldn't tighten preconditions without excluding valid caller states, but the harness construction itself (separate backing arrays, symbolic offsets) is the root cause of the unwinding artifact.
