# BUG-05 — `kapi_rename` (kapi)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/kapi.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `valid_string(path) && valid_string(newname)`

**Postcondition:** `\result == 0 || \result < 0`

## Counterexample

**Violated property:** `main.assertion.1`

**Key variable assignments:**
```
kapi.version = 0u
kapi.$pad1 = 0u
kapi.putc = ((const void (*)(char))NULL)
kapi.puts = ((const void (*)(char *))NULL)
kapi.uart_puts = ((const void (*)(char *))NULL)
kapi.getc = ((signed int (*)(void))NULL)
kapi.set_color = ((const void (*)(uint32_t, uint32_t))NULL)
kapi.clear = ((const void (*)(void))NULL)
kapi.set_cursor = ((const void (*)(signed int, signed int))NULL)
kapi.set_cursor_enabled = ((const void (*)(signed int))NULL)
kapi.print_int = ((const void (*)(signed int))NULL)
kapi.print_hex = ((const void (*)(uint32_t))NULL)
kapi.clear_to_eol = ((const void (*)(void))NULL)
kapi.clear_region = ((const void (*)(signed int, signed int, signed int, signed int))NULL)
kapi.has_key = ((signed int (*)(void))NULL)
kapi.malloc = ((const void * (*)(__CPROVER_size_t))NULL)
kapi.free = ((const void (*)(const void *))NULL)
kapi.open = ((const void * (*)(char *))NULL)
kapi.close = ((const void (*)(const void *))NULL)
kapi.read = ((signed int (*)(const void *, char *, __CPROVER_size_t, __CPROVER_size_t))NULL)
kapi.write = ((signed int (*)(const void *, char *, __CPROVER_size_t))NULL)
kapi.is_dir = ((signed int (*)(const void *))NULL)
kapi.file_size = ((signed int (*)(const void *))NULL)
kapi.create = ((const void * (*)(char *))NULL)
kapi.mkdir = ((const void * (*)(char *))NULL)
kapi.delete = ((signed int (*)(char *))NULL)
kapi.delete_dir = ((signed int (*)(char *))NULL)
kapi.delete_recursive = ((signed int (*)(char *))NULL)
kapi.rename = ((signed int (*)(char *, char *))NULL)
kapi.readdir = <symbolic struct/array — see classification.json>
kapi.set_cwd = ((signed int (*)(char *))NULL)
kapi.get_cwd = ((signed int (*)(char *, __CPROVER_size_t))NULL)
kapi.exit = ((const void (*)(signed int))NULL)
kapi.exec = ((signed int (*)(char *))NULL)
kapi.exec_args = ((signed int (*)(char *, signed int, char **))NULL)
kapi.yield = ((const void (*)(void))NULL)
kapi.spawn = ((signed int (*)(char *))NULL)
kapi.spawn_args = ((signed int (*)(char *, signed int, char **))NULL)
kapi.console_rows = ((signed int (*)(void))NULL)
kapi.console_cols = ((signed int (*)(void))NULL)
kapi.fb_base = ((uint32_t *)NULL)
kapi.fb_width = 0u
kapi.fb_height = 0u
kapi.fb_put_pixel = ((const void (*)(uint32_t, uint32_t, uint32_t))NULL)
kapi.fb_fill_rect = ((const void (*)(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t))NULL)
kapi.fb_draw_char = ((const void (*)(uint32_t, uint32_t, char, uint32_t, uint32_t))NULL)
kapi.fb_draw_string = ((const void (*)(uint32_t, uint32_t, char *, uint32_t, uint32_t))NULL)
kapi.font_data = ((uint8_t *)NULL)
kapi.mouse_get_pos = ((const void (*)(signed int *, signed int *))NULL)
kapi.mouse_get_buttons = ((uint8_t (*)(void))NULL)
kapi.mouse_poll = ((const void (*)(void))NULL)
kapi.mouse_set_pos = ((const void (*)(signed int, signed int))NULL)
kapi.mouse_get_delta = ((const void (*)(signed int *, signed int *))NULL)
kapi.window_create = ((signed int (*)(signed int, signed int, signed int, signed int, char *))NULL)
kapi.window_destroy = ((const void (*)(signed int))NULL)
kapi.window_get_buffer = ((uint32_t * (*)(signed int, signed int *, signed int *))NULL)
kapi.window_poll_event = <symbolic struct/array — see classification.json>
kapi.window_invalidate = ((const void (*)(signed int))NULL)
kapi.window_set_title = ((const void (*)(signed int, char *))NULL)
kapi.stdio_putc = ((const void (*)(char))NULL)
kapi.stdio_puts = ((const void (*)(char *))NULL)
kapi.stdio_getc = ((signed int (*)(void))NULL)
kapi.stdio_has_key = ((signed int (*)(void))NULL)
kapi.get_uptime_ticks = ((__CPROVER_size_t (*)(void))NULL)
kapi.get_mem_used = ((__CPROVER_size_t (*)(void))NULL)
kapi.get_mem_free = ((__CPROVER_size_t (*)(void))NULL)
kapi.get_timestamp = ((uint32_t (*)(void))NULL)
kapi.get_datetime = <symbolic struct/array — see classification.json>
kapi.wfi = ((const void (*)(void))NULL)
kapi.sleep_ms = ((const void (*)(uint32_t))NULL)
kapi.sound_play_wav = ((signed int (*)(const void *, uint32_t))NULL)
kapi.sound_stop = ((const void (*)(void))NULL)
kapi.sound_is_playing = ((signed int (*)(void))NULL)
kapi.sound_play_pcm = ((signed int (*)(const void *, uint32_t, uint8_t, uint32_t))NULL)
kapi.sound_play_pcm_async = ((signed int (*)(const void *, uint32_t, uint8_t, uint32_t))NULL)
kapi.sound_pause = ((const void (*)(void))NULL)
kapi.sound_resume = ((signed int (*)(void))NULL)
kapi.sound_is_paused = ((signed int (*)(void))NULL)
kapi.get_process_count = ((signed int (*)(void))NULL)
kapi.get_process_info = ((signed int (*)(signed int, char *, signed int, signed int *))NULL)
kapi.get_disk_total = ((signed int (*)(void))NULL)
kapi.get_disk_free = ((signed int (*)(void))NULL)
kapi.get_ram_total = ((__CPROVER_size_t (*)(void))NULL)
kapi.get_heap_start = ((__CPROVER_size_t (*)(void))NULL)
kapi.get_heap_end = ((__CPROVER_size_t (*)(void))NULL)
kapi.get_stack_ptr = ((__CPROVER_size_t (*)(void))NULL)
kapi.get_alloc_count = ((signed int (*)(void))NULL)
kapi.net_ping = ((signed int (*)(uint32_t, uint16_t, uint32_t))NULL)
kapi.net_poll = ((const void (*)(void))NULL)
kapi.net_get_ip = ((uint32_t (*)(void))NULL)
kapi.net_get_mac = ((const void (*)(uint8_t *))NULL)
kapi.dns_resolve = ((uint32_t (*)(char *))NULL)
kapi.tcp_connect = ((signed int (*)(uint32_t, uint16_t))NULL)
kapi.tcp_send = ((signed int (*)(signed int, const void *, uint32_t))NULL)
kapi.tcp_recv = ((signed int (*)(signed int, const void *, uint32_t))NULL)
kapi.tcp_close = ((const void (*)(signed int))NULL)
kapi.tcp_is_connected = ((signed int (*)(signed int))NULL)
kapi.tls_connect = ((signed int (*)(uint32_t, uint16_t, char *))NULL)
kapi.tls_send = ((signed int (*)(signed int, const void *, uint32_t))NULL)
kapi.tls_recv = ((signed int (*)(signed int, const void *, uint32_t))NULL)
kapi.tls_close = ((const void (*)(signed int))NULL)
kapi.tls_is_connected = ((signed int (*)(signed int))NULL)
kapi.ttf_get_glyph = ((const void * (*)(signed int, signed int, signed int))NULL)
kapi.ttf_get_advance = ((signed int (*)(signed int, signed int))NULL)
kapi.ttf_get_kerning = ((signed int (*)(signed int, signed int, signed int))NULL)
kapi.ttf_get_metrics = ((const void (*)(signed int, signed int *, signed int *, signed int *))NULL)
kapi.ttf_is_ready = ((signed int (*)(void))NULL)
kapi.led_on = ((const void (*)(void))NULL)
kapi.led_off = ((const void (*)(void))NULL)
kapi.led_toggle = ((const void (*)(void))NULL)
kapi.led_status = ((signed int (*)(void))NULL)
kapi.kill_process = ((signed int (*)(signed int))NULL)
kapi.get_cpu_name = ((char * (*)(void))NULL)
kapi.get_cpu_freq_mhz = ((uint32_t (*)(void))NULL)
kapi.get_cpu_cores = ((signed int (*)(void))NULL)
kapi.usb_device_count = ((signed int (*)(void))NULL)
kapi.usb_device_info = ((signed int (*)(signed int, uint16_t *, uint16_t *, char *, signed int))NULL)
kapi.klog_read = ((__CPROVER_size_t (*)(char *, __CPROVER_size_t, __CPROVER_size_t))NULL)
kapi.klog_size = ((__CPROVER_size_t (*)(void))NULL)
kapi.fb_has_hw_double_buffer = ((signed int (*)(void))NULL)
kapi.fb_flip = ((signed int (*)(signed int))NULL)
kapi.fb_get_backbuffer = ((uint32_t * (*)(void))NULL)
kapi.dma_available = ((signed int (*)(void))NULL)
kapi.dma_copy = ((signed int (*)(const void *, const void *, uint32_t))NULL)
kapi.dma_copy_2d = <symbolic struct/array — see classification.json>
kapi.dma_fb_copy = ((signed int (*)(uint32_t *, uint32_t *, uint32_t, uint32_t))NULL)
kapi.dma_fill = ((signed int (*)(const void *, uint32_t, uint32_t))NULL)
_path_buf = <symbolic struct/array — see classification.json>
_path_len = 0u
_path_buf[0l] = 0
_path_buf[1l] = 0
_path_buf[2l] = 0
_path_buf[3l] = 0
_path_buf[4l] = 0
path = _path_buf!0@1
_newname_buf = <symbolic struct/array — see classification.json>
_newname_len = 0u
_newname_buf[0l] = 0
_newname_buf[1l] = 0
_newname_buf[2l] = 0
_newname_buf[3l] = 0
_newname_buf[4l] = 0
newname = _newname_buf!0@1
result = 1
return_value_kapi_rename = 1
return_value_vfs_rename = 1
goto_symex$$return_value$$kapi_rename = 1
```

## Root cause

CBMC reports a `main.assertion.1` failure — a semantic / contract violation in `kapi_rename`.

**Realism checker's key concern:** No null/validity check on VFS global state before calling vfs_rename; the crash is confirmed dynamically and originates inside vfs_rename accessing uninitialized global filesystem structures, not from the input pointer values themselves.

**Validator reasoning:** 'kapi_rename' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`kapi_rename` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** No null/validity check on VFS global state before calling vfs_rename; the crash is confirmed dynamically and originates inside vfs_rename accessing uninitialized global filesystem structures, not from the input pointer values themselves.

Q1 (Can the violation TYPE occur?): Yes. `kapi_rename` is a thin wrapper that directly calls `vfs_rename(path, newname)` with no validation. The function is a public API entry point with no callers imposing preconditions. `vfs_rename` almost certainly accesses global VFS state (mount tables, inode structures, locks, etc.). If that global state is NULL or uninitialized — because the VFS subsystem was never initialized, was torn down, or has been corrupted — `vfs_rename` will dereference a null/invalid pointer and segfault. This is a classic 'missing initialization guard' vulnerability class at a public API boundary.

Q2 (Are the specific witness values achievable?): Yes. The dynamic harness confirmed a SIGSEGV with valid (non-null, empty-string) inputs. This rules out the input pointers being the cause; the crash originates inside `vfs_rename` itself, most likely from accessing uninitialized or null global VFS state. From a security perspective, an attacker who can invoke `kapi_rename` before filesystem initialization, or after a partial teardown, can trigger this crash. The counterexample uses empty strings (length 0), which is entirely achievable from real callers. The confirmed signal=SIGSEGV from the dynamic harness provides strong evidence this is a real code path, not a CBMC artifact.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
