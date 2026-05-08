# BUG-06 — `vfs_lookup` (vfs)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/vfs.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

```
kapi_open -> vfs_open_handle -> vfs_lookup
```

## Spec (LLM-generated)

**Precondition:** `requires (null(path) || valid_string(path))`

**Postcondition:** `ensures null(\result) || (valid(\result) && (\result->data == NULL || valid(\result->data)) && (\result->type == 1 || \result->type == 2))`

## Counterexample

**Violated property:** `vfs_lookup.precondition_instance.4`

**Key variable assignments:**
```
path[0] = -81 (0xAF, non-ASCII byte)
path[1] = 0   (null terminator)
cwd_path = "/"
use_fat32 = 0
normalized[0] = '/'
normalized[1] = -128 (corrupted stack memory)
normalized[2] = 2
normalized[3] = '/'
normalized[4] = 0
```

## Root cause

**This function contains TWO distinct bugs:**

**Bug A — `parts[32]` stack overflow**: The path tokenizer stores up to 32 path components in a fixed `parts[32]` stack array. A path string with 33 or more `/`-separated components (e.g., `/a/b/c/.../` with 33 parts) overflows the `parts` array into adjacent stack memory. There is no bounds check on the `depth` counter before writing to `parts[depth++]`.

**Bug B — `strtok_r` misuse with non-ASCII input**: The non-standard usage pattern `strtok_r(rest, "/", &rest)` is employed for path tokenization. When the input path contains non-ASCII bytes (such as 0xAF as in the counterexample), the tokenizer returns a pointer outside the `fullpath` buffer. The `parts` array then holds invalid pointers, and `strcat(normalized, parts[i])` reads from corrupt memory, producing the garbled `normalized` buffer observed in the counterexample.

## How to trigger

**Bug A**: Call `kapi_open("/a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q/r/s/t/u/v/w/x/y/z/aa/bb/cc/dd/ee/ff/")` — a path with 33 or more components. The 33rd write to `parts[32]` overflows into adjacent stack variables.

**Bug B**: Call `kapi_open` with a path containing a non-ASCII byte, e.g., a string where `path[0] = 0xAF`. The non-standard `strtok_r` usage pattern causes the tokenizer to return a pointer outside `fullpath`, leading to out-of-bounds reads when building the normalized path.

## Realism assessment

**Verdict:** REALISTIC

1. **Call chain analysis**: The function is reached via `kapi_open → vfs_open_handle → vfs_lookup`. This is an OS/kernel API entry point that accepts user-supplied path strings — a prime source of untrusted, arbitrary input including non-ASCII characters.

2. **Input characterization**: The counterexample supplies `path = _path_buf` where `_path_buf[0] = -81` (0xAF, a non-ASCII byte) and `_path_buf[1] = 0`. This is a valid C string (non-null, null-terminated) with a non-ASCII first character. Such inputs are entirely plausible from user space.

3. **Code path traced**: With `cwd_path = "/"`, `path[0] = 0xAF` (not '/' and not '\0'), the code takes the `snprintf(fullpath, 256, "/%s", path)` branch. The strtok_r tokenizer then processes this, yielding `parts[0]` pointing into `fullpath`, `depth = 2`.

4. **Observed corruption**: The normalized buffer ends up with `normalized[0]='/', normalized[1]=-128, normalized[2]=2, normalized[3]='/', normalized[4]=0`. These are internal memory values (not actual path content), suggesting that `strcat(normalized, parts[i])` is reading from pointer internals or corrupt stack memory.

5. **Not a false positive indicator**: The counterexample does NOT require NULL pointers, extreme integer values, or zero-initialized global state. The `cwd_path = "/"` is exactly what the real program initializes it to.

6. **Realistic exploitability**: An attacker or buggy program passing a path with high-byte characters (e.g., UTF-8 sequences or locale-specific filenames) through `kapi_open` could trigger this misbehavior in `mem_lookup`, potentially corrupting the normalized path or causing incorrect lookups.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
