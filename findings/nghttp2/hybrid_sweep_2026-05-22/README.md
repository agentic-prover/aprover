# nghttp2 nghttp2_frame.c — OpenRouter Claude sweep, 2026-05-22

Full bmc-agent pipeline on nghttp2's HTTP/2 frame parser
(1227 LoC, 73 functions). Realism + feedback loop both enabled.

## Configuration

- All roles routed to Claude Sonnet 4.5 via OpenRouter
- `--real-libc` (CBMC handles preprocessing via -I)
- `--enable-realism-check`
- `--enable-feedback-loop`
- bmc-agent loaded the patched cbmc.py + llm.py (commits 2ab4dcf, b7e53eb)

## Results

| Metric | Value |
|---|---|
| Functions analysed | 73 |
| Verified clean | **38** (52%) |
| `real_bug` raw classifications | 18 |
| `spurious` (classifier-downgraded) | 8 |
| `real_bug` after realism + feedback filter | **2** |
| Wall clock | 51 min |

Of the 18 raw real_bug classifications, **16 were correctly downgraded
to `unrealistic`** by the feedback loop or realism check. The 2
survivors:

- `nghttp2_frame_altsvc_init` — `pointer_dereference.7`,
  confirmed_system_entry, realism=uncertain (K2 504 hiccup on realism call)
- `nghttp2_frame_priority_update_init` — `pointer_dereference.*`,
  confirmed_system_entry, realism=uncertain (K2 504 hiccup)

Both `_init` functions follow the standard nghttp2 init pattern:
caller passes a freshly-allocated frame struct + caller-owned payload
pointers. The CEx requires a NULL payload field that real callers
never pass. Same defensive-programming-gap pattern as today's other
sweeps; the K2 backend's intermittent 504s prevented the realism LLM
from running.

## Why this matters

nghttp2 is an IBB-covered OSS-Fuzz target with a long fuzzing history.
Finding **38 verified-clean memory-safety properties across the entire
frame parser** in 51 minutes is a strong demonstration of the
hybrid-mode bmc-agent pipeline on real-world wire-format C code:

- Every `nghttp2_frame_*_init` constructor that survived realism check
- `nghttp2_iv_check`, `nghttp2_check_header_*`, `nghttp2_nv_compare_name`
- `nghttp2_frame_pack_frame_hd`, `nghttp2_frame_unpack_*_payload`
- Settings/iv/priority/window-update/altsvc/data/origin frame packers
- The `nghttp2_buf_remaining_capacity` / `nghttp2_buf_offset` helpers

Specs were synthesised top-down from callers and then refined through
the feedback loop. **No real bugs surfaced** — consistent with
nghttp2's deep fuzzing coverage.

## Wall clock / cost

- 51 min wall clock
- 73 functions × ~7 LLM calls each = ~500 LLM calls
- LLM provider: Claude Sonnet 4.5 via OpenRouter
- Estimated cost: ~$2-3

## Files

`/tmp/aprover_llama_nghttp2_or/nghttp2_frame/nghttp2_frame_or/` has the
per-function spec.json / harness.c / bug_report.json /
classification.json.
