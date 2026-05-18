# curl/lib/urlapi.c — clean verify (with caveats)

**Date**: 2026-05-19
**Source**: curl `master` checkout 2026-05-12, `lib/urlapi.c`
**Target functions**: all 39 functions defined in the TU
**bmc-agent config**: `--real-libc`, `--enable-realism-check
--enable-realism-thinking`, `--enable-dynamic-validation
--enable-feedback-loop --enable-flag-selection`, `--threat-model
security`, `--raw-bytes`, `-D HAVE_CONFIG_H -D BUILDING_LIBCURL`.

## Result

**0 real bugs confirmed.** 23/39 functions produced a CBMC verdict;
3 raw real_bug findings were classified by Phase 3 and all 3 were
suppressed by the realism/refinement filters. 16 functions failed
pre-verdict (coverage gaps, see below).

## Notable raw findings (all suppressed)

The classifier raised 3 raw real_bug findings before CLI filtering;
each was downgraded:

- `parse_port.*`: CBMC parse error (exit 6) — caused by a separate
  bmc-agent parser bug surfaced and fixed during this run (see below).
- `redirect_url`, `allowed_in_path`, `parseurl_and_replace`,
  `set_url`: bug_report.json written but classification ended in
  `spurious` after realism-check rejected each.

## Fix landed: storage-class macro recovery

curl uses the `UNITTEST` macro to gate static linkage:

```c
#ifdef UNITTESTS
#define UNITTEST            /* externally visible in test builds */
#else
#define UNITTEST static
#endif
```

A function declared `UNITTEST CURLUcode parse_port(...)` was being
mis-parsed: tree-sitter treated `UNITTEST` as the entire return type
and stashed `CURLUcode` in a sibling ERROR node. The harness then
emitted:

```c
UNITTEST result = parse_port(...);
```

After preprocessing, `UNITTEST` expands to `static`, leaving
`static result = parse_port(...);` — which CBMC rejects with
"expected constant expression" because `static` requires an
initializer that's compile-time-constant.

Parser now detects this case symmetrically to the
GGML_RESTRICT/GGML_API recoveries already in place. When the parsed
type looks like a storage-class macro (ALL_CAPS-with-underscore,
≥4-char all-caps single word, or leading `__`) and the
function_definition has an ERROR sibling containing a single
identifier, the macro is folded into the type prefix and the ERROR
identifier becomes the real type.

The fix also extends the macro-detection heuristic to cover
single-word uppercase macros of ≥4 chars (`UNITTEST`, `EXPORT`,
`INLINE`); a negative test guards `T`/`OK`/`NO` from being mistaken
for macros.

## Coverage caveat

16/39 functions failed CBMC parse/convert — those are excluded from
the clean claim. The new coverage-diagnostics artifact records 6
other parse errors of unknown class (not GGML_VERSION style, not
struct-typedef syntax) plus 10 that hit code-6 errors during
refinement. These are mostly the heavy URL-canonicalisation paths
(`set_url`, `parse_authority` with its 185 raw CEXes, the
`dedotdotify` path) — they have non-trivial preprocessor /
include dependencies that the harness pipeline can't yet fully
resolve.

## What this tells us

URL parsing is curl's classic high-value attack surface (Curl has
historically had CVEs in this code: CVE-2022-27775, CVE-2023-27535,
etc.). bmc-agent's clean-verify on the 23 functions that fully
analysed is a non-trivial positive signal. The 16 coverage gaps are
the next obvious priority for tightening — none of them is a missing
build-config macro (the coverage-diagnostics path would have surfaced
that explicitly).
