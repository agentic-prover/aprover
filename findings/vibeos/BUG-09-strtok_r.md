# BUG-09 — `strtok_r` (string)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/string.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
kapi_open -> vfs_open_handle -> vfs_lookup -> mem_lookup -> strtok_r
```

## Spec (LLM-generated)

**Precondition:** `requires valid_string(delim) && valid(saveptr) && (str != NULL ? valid_string(str) : (*saveptr == NULL || valid_string(*saveptr))) && (str != NULL || *saveptr != NULL || true)`

**Postcondition:** `ensures (\result == NULL || valid_string(\result)) && (\result != NULL ? valid_string(*saveptr) || *saveptr == NULL : *saveptr == NULL) && (\result != NULL ? (\result[0] != '\0' && (forall i, 0 <= i < strlen(\result) implies !is_delim(\result[i], delim))) : true)`

## Counterexample

**Violated property:** `main.assertion.2`

**Key variable assignments:**
```
str[0]    = 0 (null byte — empty string)
str[1]    = 1
str[2]    = 1
str[3]    = 1
delim[0]  = 0 (null byte — empty delimiter)
result    = NULL
saveptr   = NULL after call
```

## Root cause

The VibeOS kernel implements its own `strtok_r` with non-standard usage: `strtok_r(rest, "/", &rest)` is called from `mem_lookup`/`vfs_lookup` to tokenize path components. When the input string is empty (first byte is null) and the delimiter string is also effectively empty, `strtok_r` sets `*saveptr = NULL` and returns NULL. The caller in `vfs_lookup` asserts or assumes the returned token is non-NULL (a valid path component), triggering `main.assertion.2`. This edge case is reachable via `kapi_open` when a user passes an empty or null-component path.

## How to trigger

Call `kapi_open("")` (empty path string) or `kapi_open` with a path containing an empty component such that `strtok_r` is called with an empty `str` argument. The call chain passes the path through `vfs_lookup → mem_lookup → strtok_r`. An empty string input causes `strtok_r` to return NULL immediately, which the caller does not handle.

## Realism assessment

**Verdict:** REALISTIC

Tracing the counterexample through the function: (1) str = _str_buf where _str_buf[0] = 0 (null byte), so str is non-NULL but points to an empty string. (2) start = str; the while loop at `while (*start && is_delim(*start, delim))` doesn't execute because *start == '\0'. (3) The check `if (*start == '\0')` is true, so *saveptr = NULL and the function returns NULL. The assertion violation occurs in `mem_lookup`/`vfs_lookup` when it checks that the returned token is non-NULL, expecting a valid path component.

The call chain `kapi_open → vfs_open_handle → vfs_lookup → mem_lookup → strtok_r` is a kernel API entry point receiving user-supplied paths. An empty path string (or a path component starting with a null byte) is a plausible user-supplied input — a user could pass an empty filename or a path like "" to `kapi_open`. Additionally, an empty delimiter string (all null bytes) means no characters are ever delimiters, which combined with an empty input string causes `strtok_r` to always return NULL. This edge-case combination is reachable from untrusted user input through the kernel API boundary, making it a realistic scenario rather than a verification artifact.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
