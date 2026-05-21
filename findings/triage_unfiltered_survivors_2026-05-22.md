# Triage — 8 unfiltered real_bug survivors from 2026-05-22 sweeps

Across today's three sweeps (OR-mode Neuron, K2-hybrid P2 Neuron, OR
llama.cpp+nghttp2), 32 functions were classified `real_bug` by Phase 3.
24 of those were correctly downgraded to `unrealistic` by the
in-sweep feedback loop or the LLM realism check. The remaining **8**
have `realism_check.verdict ∈ {null, uncertain}` because the realism
LLM call either failed (8MB OpenRouter limit pre-cbmc.py-patch) or
hit K2 504 hiccups during the P2 / llama+nghttp2 runs.

This document is a manual triage of each survivor based on reading
the function body and caller sites. **All 8 are FPs.**

## Methodology

For each survivor: locate the function source, identify what state the
CEx requires, then check whether real callers can produce that state.

## Survivors

### 1. `ts_nq_destroy` (neuron_topsp.c)

Indirect call through `ndhal->ndhal_topsp.ts_nq_get_nqid(nd, eng_index, nq_type)`.
CEx requires the function-pointer field to be NULL. Real init paths
(`ndhal_register_arch`) populate this table at probe; cleanup is only
called from teardown after init succeeded. **FP** (defensive-programming
gap on indirect-call deref).

### 2. `neuron_log_rec_add` (neuron_log.c)

`pointer_dereference.13` deep in the body. The function has its own
`if (nd->log_obj.log == NULL) return;` guard at the top. CEx must
involve a state the guard already excludes — likely path-divergent
witness from CBMC's symbolic exploration. Real callers (kernel logging
sites) maintain `nd->log_obj.log` valid after `neuron_log_init`. **FP**.

### 3. `neuron_ds_release_pid` (neuron_ds.c)

```c
void neuron_ds_release_pid(struct neuron_datastore *nds, pid_t pid) {
    struct neuron_datastore_entry *entry;
    neuron_ds_acquire_lock(nds);
    if (pid == 0) pid = task_tgid_nr(current);
    entry = neuron_ds_find(nds, pid);
    if (entry != NULL) neuron_ds_release_entry(nds, entry);
    neuron_ds_release_lock(nds);
}
```

CEx requires `nds == NULL`. All in-tree callers pass `nd->datastore`
which is initialized at `neuron_init`. **FP**.

### 4. `nr_stop_thread` (neuron_reset.c)

Kernel teardown function. NULL-guards on `nd->nr.thread`, then derefs
`nd->nr.req_pending_head` / `nd->nr.req_cmpl_head` / `nd->nr.nr_lock`.
CEx requires `nr.thread != NULL` while other `nr.*` fields invalid —
impossible state in practice because `nr_create_thread` initializes
them all together. **FP**.

### 5. `ndma_zerocopy_supported` (neuron_dma.c)

```c
bool ndma_zerocopy_supported(void) {
    return !ndhal->ndhal_ndma.ndma_retry_memcpy || zerocopy_trn1_override;
}
```

Single-line function reading `ndhal->ndhal_ndma.ndma_retry_memcpy`.
CEx requires `ndhal == NULL`. ndhal is set by `ndhal_register_arch` at
driver probe and is valid for the lifetime of the module. **FP**.

### 6. `nmetric_set_performance_profile` (neuron_metrics.c)

```c
void nmetric_set_performance_profile(struct neuron_device *nd, int profile) {
    snprintf(nmetric_constant_metrics[NMETRIC_PROFILE_ID_IDX], ...,
             "%d", ndhal->ndhal_perf.current_performance_profile);
}
```

Same `ndhal != NULL` invariant — same caller-discipline. **FP**.

### 7. `nghttp2_frame_altsvc_init` (nghttp2_frame.c)

```c
void nghttp2_frame_altsvc_init(nghttp2_extension *frame, int32_t stream_id,
                               uint8_t *origin, size_t origin_len, ...) {
    nghttp2_frame_hd_init(&frame->hd, 2 + origin_len + field_value_len, ...);
    *(nghttp2_ext_altsvc *)frame->payload = ...;
}
```

CEx requires `frame == NULL` or `frame->payload == NULL`. Real
callers (e.g., `nghttp2_submit_altsvc`) heap-allocate the frame and
set `frame->payload` to a pre-allocated buffer before calling _init.
**FP** — standard nghttp2 caller pattern.

### 8. `nghttp2_frame_priority_update_init` (nghttp2_frame.c)

Same shape as #7 — caller passes a heap-allocated frame with payload
already pointing to a buffer. **FP**.

## Aggregate verdict

**0 likely-true bugs** across the 8 survivors. All match the
defensive-programming-gap pattern: callee derefs a field that the
caller invariably maintains valid (`ndhal != NULL`, `nd != NULL`,
struct fields valid after init).

This is consistent with bmc-agent's earlier findings on this code class
(ggml-alloc.c sweep 2026-05-21, jq jvp_utf8 sweep 2026-05-12). The
classifier's "confirmed_system_entry" tag is technically correct (no
caller in the same TU), but it doesn't capture the cross-TU init
discipline that the kernel + nghttp2 maintain.

## How this would close on a re-run with patched bmc-agent

With commits `2ab4dcf` (cbmc raw_output cap), `b7e53eb` (HTTP 4xx
no-retry), and a healthy K2 backend (current 504 hiccups resolve),
the realism LLM should fire on each of these 8 candidates and emit
`UNREALISTIC` with reasoning about the missing caller-side
initialization context. Estimated re-run cost: ~$0.50 in K2-hybrid
mode.
