# BUG-05 — `fat32_is_dir` (fat32)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_system_entry` |
| **Signal** | — |
| **Module** | `kernel/fat32.c` |
| **Bug type** | semantic |
| **Violated property** | `main.assertion.1` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

kernel_main → vfs_init → fat32_is_dir

## Spec (LLM-generated)

**Precondition:** `requires valid_string(path)`

**Postcondition:** `ensures (\result == 1) || (\result == 0) || (\result == -1) && (\result == -1 || \result == 0 || \result == 1) && (\result == 1 ==> the path refers to a directory entry in the FAT32 filesystem) && (\result == 0 ==> the path refers to a non-directory entry in the FAT32 filesystem) && (\result == -1 ==> the filesystem is not initialized or the path does not exist)`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
fs_initialized = 0
_path_buf = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'char', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer',...
_path_len = 1u
_path_buf[1l] = 0
_path_buf[0l] = 0
_path_buf[2l] = 0
_path_buf[3l] = 0
_path_buf[4l] = 0
path = _path_buf!0@1
result = -1
return_value_fat32_is_dir = -1
format = {'name': 'unknown'}
va_arg = _path_buf!0@1
return_value___VERIFIER_nondet_int = 0
list = ((va_list)NULL)
va_args = {'elements': [{'index': 0, 'value': {'data': 'va_arg!0', 'name': 'pointer', 'type': 'const void *'}}], 'name': 'array'}
va_args[0l] = va_arg!0
goto_symex$$return_value$$fat32_is_dir = -1
```

## Root cause / validation reasoning

Cross-file caller 'vfs_init' can reach the CEx state. Call chain: ['kernel_main', 'vfs_init', 'fat32_is_dir']. Full chain traced to system entry.

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The harness introduces an artificial NULL dereference that does not exist in the real function. The real bug class (null path passed to printf) exists but is a different code path than what the counterexample demonstrates.

Q1 (Can the violation TYPE occur in the real program?): The actual `fat32_is_dir` function properly guards against the uninitialized-filesystem case by returning -1 early. The NULL dereference shown in the harness (`root->attr` where root=NULL) does NOT exist in the real function body — the harness introduces an artificial bug for demonstration purposes. However, the real function does pass `path` directly to `printf("%s", path)` without a NULL check on `path` itself. If an attacker passes NULL as `path`, this causes undefined behavior in `printf`. Since no call sites guard against null path (call-site analysis shows this may be an external API entry point), a null `path` argument is plausible in a security context.

Q2 (Is this specific witness realistic?): The counterexample shows `path = _path_buf` (a valid non-null pointer with all zeros) and `fs_initialized = 0`. This specific path through the REAL function is completely safe — it prints a message and returns -1 with no UB. The CBMC violation is triggered by the harness code's artificial NULL dereference (`root->attr`), not by any bug in the actual function. The witness is an artifact of the harness, not of the real code.

Summary: The specific violation (harness NULL dereference of `root`) is an artifact. However, the real function has a latent null-pointer risk if `path=NULL` is passed to `printf`, which is a realistic attack vector for an unvalidated external API. The finding is uncertain — the specific counterexample is artificial, but the vulnerability class (null dereference via unchecked path parameter) is potentially real.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
