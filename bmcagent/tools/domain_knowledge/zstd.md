# zstd domain knowledge for bmc-agent

## Format

zstd compressed stream:
```
Frame = Magic(4B) + FrameHeader(2-14B) + Block[]+ + (optional) Content_Checksum(4B)
Block = BlockHeader(3B) + Block_Content(N bytes)
BlockType in {Raw, RLE, Compressed, Reserved}
Compressed_Block = Literals + Sequences
Literals = LiteralsHeader + Huffman_Tree(optional) + Huffman_Stream(1 or 4 streams)
Sequences = SequencesHeader + LL_Code + OF_Code + ML_Code + Bitstream
```

The decompressor is a bit-stream-driven state machine using FSE (Finite State
Entropy) and Huffman tables. Each table has bounded alphabet size and
bounded code lengths.

## Public API entry points (attacker-reachable)

Untrusted compressed input flows in via:
- `ZSTD_decompress(dst, dstCapacity, src, srcSize)` — one-shot
- `ZSTD_decompressDCtx(dctx, ...)` — with context
- `ZSTD_decompressStream(zds, output, input)` — streaming
- `ZSTD_decompressContinue(dctx, dst, ...)` — low-level continuation
- `ZSTD_decompressBegin(dctx)` + `ZSTD_decompressContinue` — block-by-block

Dictionary loading is also an attacker vector:
- `ZSTD_decompress_usingDict(dctx, dst, ..., dict, dictSize)` — dictionary from untrusted source

## Key type definitions

```c
typedef enum { ZSTDds_getFrameHeaderSize, ... } ZSTD_dStage;
typedef struct ZSTD_DCtx_s ZSTD_DCtx;
typedef struct ZSTD_DStream_s ZSTD_DStream; /* alias for ZSTD_DCtx */
typedef ZSTD_DCtx HUF_DTable;               /* Huffman decode table */
typedef U32 FSE_DTable;                     /* FSE decode table */
```

`size_t` return values use `ZSTD_isError(ret)` to test for error (a magic-range
value above `(size_t)-128`); otherwise `ret` is the decompressed size or
bytes-needed.

## Historical bug classes (from CVE + OSS-Fuzz history)

1. **Bit-stream decoder OOB reads** (parallel to libarchive RAR5 patterns)
   - `BIT_readBits`, `BIT_lookBits`, `BIT_reloadDStream` — read N bits from
     an in-memory bit-buffer; if N exceeds the buffer, OOB
   - Same `+N tail` structural-invariant as libarchive's `read_bits_16`/`32`
   - Check: is the buffer over-allocated to support max peek-ahead?

2. **FSE/Huffman table corruption** (less common but high impact)
   - Malformed literal-tree spec → FSE state machine reads past table bounds
   - `HUF_decompress*_usingDTable_internal` family
   - Bound: `HUF_TABLELOG_MAX` (typically 12) → alphabet ≤ 4096

3. **Sequence decoding bugs** (offset/match-length encoding)
   - LL/OF/ML codes have bounded alphabets (35/31/52)
   - Offsets above `window_size` are spec violations but parser may not check
   - `ZSTD_decodeSequence` / `ZSTD_execSequence` family

4. **Dictionary loading bugs** (CVE-2021-24032 was the headline)
   - Malformed dictionary header → uninit values used downstream
   - `ZSTD_decompressBegin_usingDict` and friends

5. **Window-size overflow** (multiple, depends on input frame header)
   - Frame header declares window_size which is upper-bounded; attacker
     can request very large values; pre-check is `ZSTD_decompressBound`

## Threat model for realism judgment

A bug is REALISTIC if:
- Reachable from `ZSTD_decompress*` with attacker-controlled `src` bytes
- The function-under-test is called from `ZSTD_decompressFrame`/
  `ZSTD_decompressBlock`/`ZSTD_decompressSequencesLong`

A bug is UNREALISTIC if:
- Requires `ZSTD_DCtx*` to be uninitialized — `ZSTD_createDCtx` allocates and
  initializes (G2 pattern, same as libarchive)
- Requires `dctx->bType` or similar enum field to be outside its valid range —
  decoded values are always within their spec
- Requires bit-stream peek-ahead to land OOB — the input buffer in
  `ZSTD_decompressContinue` is sized by caller's `srcSize`; peek-ahead is
  bounded by `BIT_*` API contract (32 bits at a time, never beyond
  `bitContainer + sizeof(BIT_DStream_t)`)

## Bug-density priority

High priority (decompression core):
1. `huf_decompress.c` — Huffman table parsing + decode
2. `zstd_decompress_block.c` — block-level decompression
3. `zstd_decompress.c` — frame-level entry points
4. `zstd_ddict.c` — dictionary loading

Lower priority:
- `fse_decompress.c` — FSE decompression (called by huf_decompress, well-tested)
- `huf_compress.c` etc. — compression side (not attacker-reachable from untrusted input)
- `zstd_v01.c`-`v07.c` — legacy format compatibility (out of standard rotation)

## Specific spec hints

- Most decompression functions return `size_t` where `ZSTD_isError(ret)` means
  "magic error sentinel value." Postconditions should account for both
  normal and error returns.
- `dctx->litPtr` and `dctx->litEnd` bound the literal buffer; reads outside
  this range are bugs.
- Bit-reader `BIT_DStream_t` has invariants:
  - `bitContainer` holds up to 64 bits
  - `bitsConsumed <= 64`
  - `ptr >= start` and `ptr <= limitPtr`
- Huffman alphabet size is bounded by `HUF_TABLELOG_MAX = 12` → max symbol
  index `< (1 << 12) = 4096`.

## Known FPs to suppress

- `dctx->X` field NULL when called from harness: G2 — `ZSTD_createDCtx`/
  `ZSTD_initDStream` initializes everything.
- `BIT_DStream_t` fields outside invariant: G4 — `BIT_initDStream` establishes
  the invariant from the caller.
- Bit-stream peek N bits when `bitsConsumed + N > 64`: G3 — the bit-reader
  refills via `BIT_reloadDStream` between use cycles.
