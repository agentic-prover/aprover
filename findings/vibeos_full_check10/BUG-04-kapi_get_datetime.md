# BUG-04 — `kapi_get_datetime` (kapi)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGSEGV |
| **Module** | `kernel/kapi.c` |
| **Bug type** | semantic |
| **Violated property** | `main.assertion.1` |
| **Realism** | realistic (high confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

Direct entry (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires (null(year) || valid(year)) && (null(month) || valid(month)) && (null(day) || valid(day)) && (null(hour) || valid(hour)) && (null(minute) || valid(minute)) && (null(second) || valid(second)) && (null(weekday) || valid(weekday))`

**Postcondition:** `ensures (null(year) || (*year >= 0)) && (null(month) || (*month >= 1 && *month <= 12)) && (null(day) || (*day >= 1 && *day <= 31)) && (null(hour) || (*hour >= 0 && *hour <= 23)) && (null(minute) || (*minute >= 0 && *minute <= 59)) && (null(second) || (*second >= 0 && *second <= 59)) && (null(weekday) || (*weekday >= 0 && *weekday <= 6))`

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
kapi.readdir = ((signed int (*)(const void *, signed int, char *, __CPROVER_size_t, uint8_t *))NULL)
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
kapi.window_poll_event = ((signed int (*)(signed int, signed int *, signed int *, signed int *, signed int *))NULL)
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
kapi.get_datetime = ((const void (*)(signed int *, signed int *, signed int *, signed int *, signed int *, signed int *, signed int *))NULL)
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
kapi.dma_copy_2d = ((signed int (*)(const void *, uint32_t, const void *, uint32_t, uint32_t, uint32_t))NULL)
kapi.dma_fb_copy = ((signed int (*)(uint32_t *, uint32_t *, uint32_t, uint32_t))NULL)
kapi.dma_fill = ((signed int (*)(const void *, uint32_t, uint32_t))NULL)
_year_val = 0
year = _year_val!0@1
_month_val = 0
month = _month_val!0@1
_day_val = 0
day = _day_val!0@1
_hour_val = 0
hour = _hour_val!0@1
_minute_val = 0
minute = _minute_val!0@1
_second_val = 0
second = _second_val!0@1
_weekday_val = 0
weekday = _weekday_val!0@1
dt = {'members': [{'name': 'year', 'value': {'binary': '00000000000000000000000000000000', 'data': '0', 'name': 'integer', 'type': 'signed int', 'width': 32}}, {'name': 'month', 'value': {'binary': '000...
```

## Root cause / validation reasoning

'kapi_get_datetime' is an entry function (no callers in any file). The counterexample is directly reachable from the system boundary.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGSEGV`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGSEGV

## Realism assessment

**Verdict:** REALISTIC (high confidence)

**Key concern:** None — both the violation type and the specific path are realistic given the confirmed SIGSEGV in the dynamic harness.

Q1 — Can the violation TYPE occur? Yes. The function under test calls `rtc_get_datetime(&dt)` unconditionally before any guards. If `rtc_get_datetime` is implemented as a function pointer (as suggested by the `kapi` struct pattern in the codebase) that could be NULL, or if it accesses memory-mapped hardware registers that are not properly initialized or mapped, a null/invalid pointer dereference is possible. This is a public API entry point with no precondition validation, and all `kapi` function pointers are NULL in the counterexample, suggesting the broader codebase heavily uses nullable function pointers. In the real program, `rtc_get_datetime` could be backed by such a pointer that is NULL when the RTC peripheral is not initialized. Q2 — Are the witness values realistic? The dynamic harness independently confirmed a SIGSEGV, which is strong evidence the fault is reproducible in practice. The SIGSEGV is consistent with calling through a NULL function pointer or accessing unmapped hardware registers. The counterexample shows all `kapi` function pointers as NULL, which in an uninitialized or incorrectly initialized kernel API context is entirely plausible. The confirmed signal in the dynamic validation makes this a high-confidence real fault: the violation type (dereference of an invalid/NULL function pointer or uninitialized hardware region via `rtc_get_datetime`) is triggerable when called before the RTC subsystem is properly initialized.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
