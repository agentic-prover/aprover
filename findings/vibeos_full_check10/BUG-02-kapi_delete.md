# BUG-02 — `kapi_delete` (kapi)

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

**Precondition:** `valid_string(path)`

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
_path_len = 1u
_path_buf[1l] = 0
_path_buf[0l] = 0
_path_buf[2l] = 0
_path_buf[3l] = 0
_path_buf[4l] = 0
path = _path_buf!0@1
result = 1
return_value_kapi_delete = 1
return_value_vfs_delete = 1
goto_symex$$return_value$$kapi_delete = 1
```

## Root cause

CBMC reports a `main.assertion.1` failure — a semantic / contract violation in `kapi_delete`.

**Validator reasoning:** 'kapi_delete' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## How to trigger

`kapi_delete` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

A standalone GCC-compiled reproducer was generated and executed; it crashed with `SIGSEGV`. The reproducer source is preserved in the run's `classification.json` under `dynamic_result.harness_source`.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

Q1 (Can the violation TYPE occur?): Yes. `kapi_delete` calls `vfs_delete(path)`, and from the counterexample context, `vfs_delete` appears to dispatch through the `kapi` global function-pointer table. The counterexample shows `kapi.delete = NULL`. If `vfs_delete` internally calls through `kapi.delete` (or any other NULL function pointer in the struct), a null function pointer dereference (SIGSEGV) will occur at runtime. Since `kapi_delete` is a public/system entry point with no callers shown — meaning there is no guaranteed initialization guard — this path is reachable whenever `kapi` is not properly initialized before `kapi_delete` is invoked. Q2 (Is this witness realistic?): Yes. The dynamic harness independently confirmed SIGSEGV, removing any doubt about whether this is a CBMC artifact. The scenario of calling `kapi_delete` before or without initializing the `kapi` vtable is realistic in OS/embedded contexts where initialization ordering may not be enforced. An attacker or erroneous caller triggering this via the public API before initialization completes is a plausible exploitation scenario.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
