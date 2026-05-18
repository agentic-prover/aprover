# curl/lib/parsedate.c — clean verify

**Date**: 2026-05-19
**Source**: curl `master` checkout 2026-05-12, `lib/parsedate.c`
**Target functions**: all 17 functions defined in the TU
**bmc-agent config**: `--real-libc`, `--enable-realism-check
--enable-realism-thinking`, `--enable-dynamic-validation
--enable-feedback-loop --enable-flag-selection`, `--threat-model
security`, `-D HAVE_CONFIG_H -D BUILDING_LIBCURL`.

## Result

**0 real bugs.** 17/17 functions produced a CBMC verdict — full
coverage, no parse errors. All 12 raw counterexamples were classified
as spurious by the realism check / refinement loop.

## How the run unfolded

### v1 (no -I for build/lib): blocked
CBMC failed 17/17 with `curl_config.h: No such file or directory`.
The new coverage-diagnostics path flagged the run as BLOCKED.

### v2 (with `-I /tmp/curl_src/build/lib`): 2 spurious confirmed_system_entry findings
The 17 functions all parsed and verified. Phase 3 raised 2 raw
findings:

- `datenum.oneortwodigit.pointer_dereference.5` (confirmed_system_entry)
- `time2epoch.array_bounds.1` (confirmed_system_entry)

Both reasoned: *"is an entry function (no callers in any file). The
counterexample is directly reachable from the system boundary."*

Both wrong — both functions are called by `parsedate` in the same
file. Investigation showed the call-graph for `parsedate` was empty:

```
parsedate callees: set()
```

while the source clearly has `rc = datenum(indate, &date, &w, ...)`
and `seconds = time2epoch(&w)`.

### Root cause: tree-sitter parses both branches of #ifdef

`lib/parsedate.c:99-579` defines `parsedate` twice:

```c
#ifndef CURL_DISABLE_PARSEDATE
static int parsedate(const char *date, time_t *output) {
    /* ... 30 statements, calls datenum/time2epoch/... ... */
}
#else
static int parsedate(const char *date, time_t *output) {
    (void)date; *output = 0; return PARSEDATE_OK;  /* a lie */
}
#endif
```

Tree-sitter does NOT process the preprocessor — it parses both
function bodies. bmc-agent's parser stored each new definition over
the previous one, so the 3-statement stub overwrote the real body.
The call graph then had no edges out of `parsedate`, and every
function `parsedate` actually called looked caller-less to the
classifier.

### Fix landed
`parser.py`: when a function name is seen a second time, keep the
entry with the longer body (stubs are short; real impls are
multi-statement). First-wins on ties so file-order doesn't matter.

### v3 (with fix): clean verify
After the fix:

```
Callers of 'time2epoch': ['parsedate']
Callers of 'datenum':    ['parsedate']
Callers of 'parsedate':  ['curl_getdate', 'Curl_getdate_capped']
```

The previously-confirmed-system-entry findings now run through
proper caller-reachability: realism filtered both as spurious
(internal precondition violations the caller obeys).

Final tally: 0 confirmed, 12 spurious, 5 verified-clean, 0 unresolved.

## Coverage

Every function in the TU produced a verdict. No parse errors. No
coverage-diagnostics warnings. This is a genuine clean-verify.

## Test coverage

2 new regression tests:

- `test_parser_prefers_longer_function_body_on_duplicate` — positive
  case with `#ifdef/#else/#endif` and asserts the longer body is
  retained and its callees are in the graph.
- `test_parser_keeps_first_definition_when_second_is_same_length` —
  determinism guard so identical-length duplicates don't flip on
  file order.

## What this tells us

The duplicate-definition pattern is endemic to C codebases that
support feature toggles (curl's `CURL_DISABLE_*`, OpenSSL's
`OPENSSL_NO_*`, libxml2's `LIBXML_*_ENABLED`). Every such codebase
would have been silently mis-classifying static helpers as system
entry points before this fix. The improvement generalises far
beyond curl.

curl's date parser is well-defended: the internal helpers
(`datenum`, `time2epoch`, `oneortwodigit`, `match_time`) all assume
their static callers obey the documented preconditions, and the
`Curl_getdate_capped` entry point validates input correctly. CBMC
exploring nondet inputs trips the internal asserts, but realism
correctly identifies them as caller-contract issues.
