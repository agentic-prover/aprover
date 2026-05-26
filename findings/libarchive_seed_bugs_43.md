# Libarchive seed bugs — full 43-bug list (user-supplied 2026-05-24)

Authoritative ground-truth list for the b_start..b_end interval at
`67830f7b9c27080c0170bcd71d94fb42316c47dd`.

| # | Description | Commit |
|--:|---|---|
| 1 | 7zip SEGV in SFX header checking via ELF offset validation | `24cb0b58` |
| 2 | NULL malloc() result causing memcpy(NULL,...) SIGSEGV in signature handling | `45ec1a24` |
| 3 | Double-free in libarchive_linkify_fuzzer | `070062a1` |
| 4 | RAR5 reader potential memory leak | `f8fea386` |
| 5 | RAR5 reader SIGSEGV when archive_read_support_format_rar5 called twice | `35877523` |
| 6 | CAB reader memory leak on repeated archive_read_support_format_cab calls | `e19ef42d` |
| 7 | ISO9660 parse_rockridge_ZF1() validation bug for pz_log2_bs | `c3cb1c56` |
| 8 | RAR LZSS window size mismatch after PPMd block | `d379dc0b` |
| 9 | RAR5 decompression infinite loop | `ef53e202` |
| 10 | NULL pointer dereference in CAB parser during skip | `32b62cf7` |
| 11 | OOB read in contrib/untar.c::parseoct | `00640329` |
| 12 | NULL pointer dereference in archive_acl_from_text_nl | `4b3ba035` |
| 13 | Heap OOB write in CAB LZX decoder | `79a0787b` |
| 14 | cpio -R memory leak | `393d6868` |
| 15 | Double-free in link resolver | `23edf569` |
| 16 | ISO9660 overlapping-memory handling bug fixed with memmove | `8ba3972e` |
| 17 | ISO9660 ../../ path normalization bug | `941e32fd` |
| 18 | CAB reader use of uninitialized Huffman-table values | `1f545457` |
| 19 | SIGSEGV in compress filter when appended before archive open | `3d4871e4` |
| 20 | 7zip malicious file-count sanity check | `51cfd615` |
| 21 | Joliet pathname buffer overflow | `750e8d7b` |
| 22 | ACL buffer overrun and wrong output for NULL-name ACL entries | `d45b5b4b` |
| 23 | RAR5 infinite loop in header parsing | `25d97315` |
| 24 | 7zip 32-bit heap overflow | `a4b3f692` |
| 25 | Additional 7zip 32-bit truncation bugs | `f52a211f` |
| 26 | ACL parser out-of-bounds read | `8308b61c` |
| 27 | Pathmatch heap buffer over-read | `4cbf9582` |
| 28 | XAR undefined behavior bugs | `e35b629f` |
| 29 | CPIO reader pathname validation bug in record_hardlink | `16ad9310` |
| 30 | ISO9660 NULL dereference and Joliet ID overflow | `a403da94` |
| 31 | MTREE NULL pointer dereference during archive close | `266e3d5f` |
| 32 | Sparse-file use-after-free in sparse_reset | `b1622a8e` |
| 33 | archive_match call-stack overflow | `470379a9` |
| 34 | MTREE time-value parser truncation | `0a6f7f1c` |
| 35 | ISO9660 infinite loop in Joliet ID generation | `2b0ab5bd` |
| 36 | MTREE hex parser bug | `b2ce282d` |
| 37 | ISO9660 OOB in Joliet ID generation | `a9d2cc5e` |
| 38 | ISO9660 memory leaks on error paths | `35befb8c` |
| 39 | XAR integer overflows in atou64 | `4f2d7832` |
| 40 | Unchecked calloc() result in RAR table allocation | `059dff39` |
| 41 | Unchecked calloc() results in RAR5 init_unpack | `620bdafa` |
| 42 | Oversized CPIO pathname rejection before read-ahead | `1f2da75f` |
| 43 | Windows GetTempPathW TOCTOU race condition | `a932ffa3` |
