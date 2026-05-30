# bzip2 domain knowledge for bmc-agent

## Format

bzip2 compressed stream:
```
File = Magic("BZh") + BlockSize_char + Compressed_Blocks + EndMagic + CRC32
Block = BlockMagic(6B) + BlockCRC32(4B) + RandomBit(1b) + StartPtr(24b)
      + Mapping + SelectorList + Huffman_Trees + Decoded_Stream
```

Decompression uses Huffman trees with up to `MAX_ALPHA_SIZE = 258` symbols
and `MAX_CODE_LEN = 23` bits per code. Selector list assigns one of up to
`N_GROUPS = 6` Huffman tables per 50-byte segment.

## Public API entry points (attacker-reachable)

- `BZ2_bzDecompressInit(strm, verbosity, small)`
- `BZ2_bzDecompress(strm)` — incremental
- `BZ2_bzDecompressEnd(strm)`
- `BZ2_bzRead/BZ2_bzReadOpen/BZ2_bzReadClose` — high-level file API
- `BZ2_bzBuffToBuffDecompress` — one-shot buffer decompression

`bz_stream` is the user-facing state, populated by `bzDecompressInit`.
`DState` is the internal state, allocated by `bzDecompressInit` if `small=0`.

## Key type definitions

```c
typedef struct {
    char     *next_in, *next_out;
    unsigned int avail_in, avail_out;
    unsigned int total_in_lo32, total_in_hi32, total_out_lo32, total_out_hi32;
    void     *state;                      /* DState */
    void     *(*bzalloc)(void *, int, int);
    void     (*bzfree)(void *, void *);
    void     *opaque;
} bz_stream;

typedef struct {
    bz_stream *strm;
    Int32      state;                     /* one of BZ_X_* state codes */
    UChar      state_out_ch;
    Int32      state_out_len;
    /* Block buffer + Huffman tables */
    UChar     *tt;                        /* working area */
    UInt32    *ttDArray;                  /* small-format path */
    /* Many fields populated during block header parse */
} DState;
```

## Historical bug classes

bzip2 is very mature (last new CVE ~2016 in the lib itself), so the bug
surface is narrower than libtiff/zstd. Historical issues:

1. **OOB read in Huffman decode tables** (CVE-2016-3189 was use-after-free
   in `bzip2recover`, not the library, but illustrates the codepath)
   - `bz/huffman.c`'s decoded values index into `mtfa[]` and `ftab[]`
   - Malformed selectorList → invalid table dispatch → wrong-table decode → OOB

2. **Integer overflow in block-size calculation**
   - `(BlockSize_char - '0') * 100000` for block-size in bytes
   - Pre-check should bound BlockSize_char to '1'..'9'

3. **State-machine bugs in incremental decompressor**
   - `BZ2_decompress` has many goto/state transitions; selector index out of
     range in malformed streams

## Threat model

REALISTIC: bug reachable from `BZ2_bzDecompress` with attacker-controlled bytes
written to `strm->next_in`.

UNREALISTIC:
- `strm == NULL` — `BZ2_bzDecompressInit` rejects this
- `strm->state == NULL` — initialized by `init`
- `s->state == BZ_X_*` outside enum — state values only assigned from
  literal constants in the state machine

## Bug-density priority

1. `decompress.c` — main decompression state machine, attacker reachable
2. `huffman.c` — Huffman table parsing
3. `bzlib.c` — public-API entry points
4. `randtable.c`, `crctable.c` — lookup tables, trivial

Skip: `compress.c`, `blocksort.c` (compression side, not attacker-reachable
from untrusted input).

## Specific spec hints

- `nblock_used` and `state_out_len` are session-state counters bounded by
  buffer sizes.
- `mtfa[]` indices in `BZ2_decompress` are bounded by `MAX_ALPHA_SIZE = 258`.
- `selectorMtf[]` and `selector[]` arrays are bounded by `BZ_MAX_SELECTORS = 18002`.
- The "mapping" stage reads up to 16 bits and produces in-table indices.
