# libtiff domain knowledge for bmc-agent

## Format

TIFF is a tagged image file format with a hierarchical structure:

```
TIFF file = Header(8B) -> IFD[0] -> IFD[1] -> ... -> IFD[N]
IFD = entry_count(2B) + entries[entry_count] + next_IFD_offset(4B or 8B)
Entry = tag(2B) + type(2B) + count(4B or 8B) + value_offset(4B or 8B)
```

BigTIFF uses 8-byte offsets (uint64); classic TIFF uses 4-byte (uint32).
`tif->tif_flags & TIFF_BIGTIFF` distinguishes the modes.

## Public API entry points (attacker-reachable)

Untrusted .tif file flows in via these:
- `TIFFOpen(filename, mode)` / `TIFFFdOpen(fd, ...)` / `TIFFClientOpen(...)`
- `TIFFReadDirectory(tif)` — parses an IFD, populates `tif->tif_dir`
- `TIFFReadEncodedStrip` / `TIFFReadEncodedTile` — invokes per-compression decoders
- `TIFFRGBAImageGet` — high-level raster decode
- `TIFFGetField` / `TIFFSetField` — variadic, type-discriminated

Internal helpers like `_TIFFsetByteArray`, `_TIFFmalloc`, `_TIFFsetXXXArray` are
called from many public-API paths and are realistic.

## Key type definitions (post-corpus-prep substitutions)

```c
typedef signed long tmsize_t;          /* signed size (TIFF_SSIZE_T) */
typedef unsigned long tsize_t;         /* unsigned size (deprecated alias) */
typedef uint64_t toff_t;               /* file offset */
typedef int32_t ttile_t;               /* tile number */
typedef int32_t tstrip_t;              /* strip number */
typedef uint32_t tdir_t;               /* directory number */
```

`tif->tif_dir.td_*` fields are populated by `TIFFReadDirectory` after IFD parse.
`tif->tif_stripoffset`, `tif->tif_stripbytecount` are arrays sized by strip count.

## Historical bug classes (from CVE database + OSS-Fuzz)

The libtiff bug history clusters tightly into these classes — use them as the
PRIOR when judging CEx realism:

1. **Integer overflow in size calculations** (CVE-2018-7456, -8905, -10963, etc.)
   - `td_imagewidth * td_imagelength * td_bitspersample / 8 + slack` style
   - Multiplication of attacker-controlled tag values before allocation
   - Look at: `_TIFFCheckRealloc`, `_TIFFCheckMalloc`, every `TIFFmalloc(N*M)` call

2. **Heap overflow in tag-value buffers** (CVE-2017-9935, -18013, -5321 family)
   - `TIFFTAG_INKNAMES`, `TIFFTAG_PAGENUMBER`, `TIFFTAG_DOTRANGE` tag parsers
   - Tag count or value larger than buffer; off-by-one on terminator
   - Look at: `_TIFFVSetField`, `setExtraSamples`, per-tag setters

3. **OOB read in decompression state machines** (multiple CCITT-related CVEs)
   - CCITT fax decoders (`tif_fax3.c`, `tif_fax3sm.c`) have a state-machine
     with tables; malformed bitstreams advance past table bounds
   - Old-JPEG (`tif_ojpeg.c`) decoder has known UB
   - LZW (`tif_lzw.c`) end-of-data handling has historical issues

4. **NULL deref on malformed IFD** (CVE-2019-7663, -14973, etc.)
   - `tif->tif_dir.td_X == NULL` because tag wasn't present
   - Tag-specific getters assume the tag was set

5. **Use-after-free across TIFFClose** (CVE-2017-13726 family)
   - Codec cleanup frees codec state; subsequent calls dereference stale ptr

6. **Stack overflow via deeply-nested IFD chains** (CVE-2014-9655 family)
   - Recursive `TIFFReadDirectory` on circular IFDs; unbounded recursion

## Threat model for realism judgment

A bug is REALISTIC if:
- The CEx state is reachable by feeding a crafted .tif via `TIFFOpen` + read API
- OR the function-under-test is invoked by such a path

A bug is UNREALISTIC if:
- The CEx requires a struct field to be uninitialized that `TIFFOpen` always initializes
- The CEx requires a pointer to be NULL that `TIFFReadDirectory` always sets non-NULL
- The CEx requires `tif_mode` to be a value outside `{O_RDONLY, O_RDWR}`

`tif->tif_clientdata` is opaque (user-provided), so CExes involving it
should reach the function via a public path that runs through `TIFFOpen`.

## Bug-density priority order for the sweep

When LLM budget is limited, audit these files first (high CVE-per-LOC):
1. `tif_dirread.c` / `tif_dirwrite.c` — IFD parser (CVE hotspot)
2. `tif_jpeg.c`, `tif_ojpeg.c` — Old-JPEG decoder (known UB)
3. `tif_lzw.c` — LZW decoder
4. `tif_fax3.c`, `tif_fax3sm.c` — CCITT decoder
5. `tif_predict.c` — prediction filter
6. `tif_dir.c` — tag manipulation
7. `tif_jbig.c`, `tif_zip.c`, `tif_zstd.c` — wrappers around external decoders

Lower priority: `tif_open.c`, `tif_close.c`, `tif_aux.c`, `tif_codec.c` —
mostly bookkeeping with low bug density.

## Specific spec hints for common patterns

- Functions taking `(TIFF *tif, ...)` always have `tif != NULL` from in-tree
  callers — only the test harness can produce NULL.
- Functions taking `(TIFF *tif, tdir_t dir, ...)` require `dir < tif->tif_nsubifd + 1`
- `tmsize_t` parameters are typically non-negative; `tmsize_t < 0` often means
  "I/O error" from a callback. Both code paths should be checked.
- `uint16/uint32 count` parameters representing tag-value counts should be
  bounded by the format's per-tag spec (e.g., `TIFFTAG_BITSPERSAMPLE` count
  matches `samples_per_pixel`, typically 1-4).

## Known FPs to suppress (G1-G5 + custom)

- `tif->tif_clientdata` being a "garbage pointer" is G2 — every `TIFFOpen`
  variant initializes it before the harness reaches the function.
- Functions called via `tif->tif_close`/`tif->tif_seekproc`/`tif->tif_writeproc`
  callbacks have caller-established preconditions (G4) — the callbacks are
  initialized at `TIFFOpen` time and the codec setup paths.
