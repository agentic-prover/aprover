# BUG-02 — `kbd_ring_push` (usb_hid)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_dynamic` |
| **Signal** | SIGABRT |
| **Module** | `kernel/usb_hid.c` |
| **Bug type** | semantic |
| **Violated property** | `kbd_ring_push.precondition_instance.2` |
| **Realism** | uncertain (medium confidence) |
| **Status** | ☐ Unreviewed |

## Call chain

usb_irq_handler → kbd_ring_push

## Spec (LLM-generated)

**Precondition:** `requires valid_range(report, 0, 8) && (report points to a readable 8-byte USB HID keyboard report buffer that has been cache-invalidated prior to this call)`

**Postcondition:** `ensures memory safety: no out-of-bounds access to kbd_ring.reports or kbd_ring.head/tail occurs; if the ring buffer was not full (i.e., (kbd_ring.head + 1) % 16 != kbd_ring.tail before the call), then the 8 bytes from report have been copied into kbd_ring.reports[old_kbd_ring.head] and kbd_ring.head has been advanced to (old_kbd_ring.head + 1) % 16, making the report available for consumption; if the ring buffer was full, the call is a no-op and no memory is modified; in all cases kbd_ring internal state remains consistent (head and tail are in [0,15], the modular arithmetic does not overflow, and no undefined behaviour occurs)`

## Counterexample

**Violated property:** `kbd_ring_push.precondition_instance.2`

**Key variable assignments:**
```
kbd_ring.reports = {'elements': [{'index': 0, 'value': {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '000...
kbd_ring.reports[0l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[0l][0l] = 0
kbd_ring.reports[0l][1l] = 0
kbd_ring.reports[0l][2l] = 0
kbd_ring.reports[0l][3l] = 0
kbd_ring.reports[0l][4l] = 0
kbd_ring.reports[0l][5l] = 0
kbd_ring.reports[0l][6l] = 0
kbd_ring.reports[0l][7l] = 0
kbd_ring.reports[1l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[1l][0l] = 0
kbd_ring.reports[1l][1l] = 0
kbd_ring.reports[1l][2l] = 0
kbd_ring.reports[1l][3l] = 0
kbd_ring.reports[1l][4l] = 0
kbd_ring.reports[1l][5l] = 0
kbd_ring.reports[1l][6l] = 0
kbd_ring.reports[1l][7l] = 0
kbd_ring.reports[2l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[2l][0l] = 0
kbd_ring.reports[2l][1l] = 0
kbd_ring.reports[2l][2l] = 0
kbd_ring.reports[2l][3l] = 0
kbd_ring.reports[2l][4l] = 0
kbd_ring.reports[2l][5l] = 0
kbd_ring.reports[2l][6l] = 0
kbd_ring.reports[2l][7l] = 0
kbd_ring.reports[3l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[3l][0l] = 0
kbd_ring.reports[3l][1l] = 0
kbd_ring.reports[3l][2l] = 0
kbd_ring.reports[3l][3l] = 0
kbd_ring.reports[3l][4l] = 0
kbd_ring.reports[3l][5l] = 0
kbd_ring.reports[3l][6l] = 0
kbd_ring.reports[3l][7l] = 0
kbd_ring.reports[4l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[4l][0l] = 0
kbd_ring.reports[4l][1l] = 0
kbd_ring.reports[4l][2l] = 0
kbd_ring.reports[4l][3l] = 0
kbd_ring.reports[4l][4l] = 0
kbd_ring.reports[4l][5l] = 0
kbd_ring.reports[4l][6l] = 0
kbd_ring.reports[4l][7l] = 0
kbd_ring.reports[5l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[5l][0l] = 0
kbd_ring.reports[5l][1l] = 0
kbd_ring.reports[5l][2l] = 0
kbd_ring.reports[5l][3l] = 0
kbd_ring.reports[5l][4l] = 0
kbd_ring.reports[5l][5l] = 0
kbd_ring.reports[5l][6l] = 0
kbd_ring.reports[5l][7l] = 0
kbd_ring.reports[6l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[6l][0l] = 0
kbd_ring.reports[6l][1l] = 0
kbd_ring.reports[6l][2l] = 0
kbd_ring.reports[6l][3l] = 0
kbd_ring.reports[6l][4l] = 0
kbd_ring.reports[6l][5l] = 0
kbd_ring.reports[6l][6l] = 0
kbd_ring.reports[6l][7l] = 0
kbd_ring.reports[7l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[7l][0l] = 0
kbd_ring.reports[7l][1l] = 0
kbd_ring.reports[7l][2l] = 0
kbd_ring.reports[7l][3l] = 0
kbd_ring.reports[7l][4l] = 0
kbd_ring.reports[7l][5l] = 0
kbd_ring.reports[7l][6l] = 0
kbd_ring.reports[7l][7l] = 0
kbd_ring.reports[8l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[8l][0l] = 0
kbd_ring.reports[8l][1l] = 0
kbd_ring.reports[8l][2l] = 0
kbd_ring.reports[8l][3l] = 0
kbd_ring.reports[8l][4l] = 0
kbd_ring.reports[8l][5l] = 0
kbd_ring.reports[8l][6l] = 0
kbd_ring.reports[8l][7l] = 0
kbd_ring.reports[9l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[9l][0l] = 0
kbd_ring.reports[9l][1l] = 0
kbd_ring.reports[9l][2l] = 0
kbd_ring.reports[9l][3l] = 0
kbd_ring.reports[9l][4l] = 0
kbd_ring.reports[9l][5l] = 0
kbd_ring.reports[9l][6l] = 0
kbd_ring.reports[9l][7l] = 0
kbd_ring.reports[10l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[10l][0l] = 0
kbd_ring.reports[10l][1l] = 0
kbd_ring.reports[10l][2l] = 0
kbd_ring.reports[10l][3l] = 0
kbd_ring.reports[10l][4l] = 0
kbd_ring.reports[10l][5l] = 0
kbd_ring.reports[10l][6l] = 0
kbd_ring.reports[10l][7l] = 0
kbd_ring.reports[11l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[11l][0l] = 0
kbd_ring.reports[11l][1l] = 0
kbd_ring.reports[11l][2l] = 0
kbd_ring.reports[11l][3l] = 0
kbd_ring.reports[11l][4l] = 0
kbd_ring.reports[11l][5l] = 0
kbd_ring.reports[11l][6l] = 0
kbd_ring.reports[11l][7l] = 0
kbd_ring.reports[12l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[12l][0l] = 0
kbd_ring.reports[12l][1l] = 0
kbd_ring.reports[12l][2l] = 0
kbd_ring.reports[12l][3l] = 0
kbd_ring.reports[12l][4l] = 0
kbd_ring.reports[12l][5l] = 0
kbd_ring.reports[12l][6l] = 0
kbd_ring.reports[12l][7l] = 0
kbd_ring.reports[13l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[13l][0l] = 0
kbd_ring.reports[13l][1l] = 0
kbd_ring.reports[13l][2l] = 0
kbd_ring.reports[13l][3l] = 0
kbd_ring.reports[13l][4l] = 0
kbd_ring.reports[13l][5l] = 0
kbd_ring.reports[13l][6l] = 0
kbd_ring.reports[13l][7l] = 0
kbd_ring.reports[14l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[14l][0l] = 0
kbd_ring.reports[14l][1l] = 0
kbd_ring.reports[14l][2l] = 0
kbd_ring.reports[14l][3l] = 0
kbd_ring.reports[14l][4l] = 0
kbd_ring.reports[14l][5l] = 0
kbd_ring.reports[14l][6l] = 0
kbd_ring.reports[14l][7l] = 0
kbd_ring.reports[15l] = {'elements': [{'index': 0, 'value': {'binary': '00000000', 'data': '0', 'name': 'integer', 'type': 'uint8_t', 'width': 8}}, {'index': 1, 'value': {'binary': '00000000', 'data': '0', 'name': 'intege...
kbd_ring.reports[15l][0l] = 0
kbd_ring.reports[15l][1l] = 0
kbd_ring.reports[15l][2l] = 0
kbd_ring.reports[15l][3l] = 0
kbd_ring.reports[15l][4l] = 0
kbd_ring.reports[15l][5l] = 0
kbd_ring.reports[15l][6l] = 0
kbd_ring.reports[15l][7l] = 0
kbd_ring.head = 0
kbd_ring.tail = 0
_report_val = 0
report = _report_val!0@1
next = 1
```

## Root cause / validation reasoning

Counterexample state is reachable from caller(s): ['usb_irq_handler']. Call chain: ['usb_irq_handler', 'kbd_ring_push']. Full chain traced to system entry.

## Dynamic confirmation

A standalone GCC-compiled reproducer was executed and crashed with `SIGABRT`. Dynamic harness confirmed fault: DYNAMIC:CONFIRMED signal=SIGABRT

## Realism assessment

**Verdict:** UNCERTAIN (medium confidence)

**Key concern:** The only real call site passes 'intr_dma_buffer', a static DMA buffer that should always be non-null and 8 bytes long, so the specific null/invalid-pointer witness from CBMC cannot be reached via normal callers. The dynamic SIGABRT was produced by an artificial assert in the harness about ring-full overflow, not the reported precondition violation, making the dynamic confirmation misleading.

Q1 — Can the violation TYPE occur? The violated property is 'precondition_instance.2', most likely a validity/range check on the 'report' pointer passed to memcpy. The function itself cannot verify that 'report' points to at least 8 readable bytes; if a caller passes a null or short buffer, memcpy would corrupt memory or fault. In principle this class of violation (invalid pointer into memcpy) is real. Q2 — Is the specific witness realistic? The counterexample shows 'report = _report_val!0@1', a CBMC non-deterministic symbolic value that has not been constrained to non-null. At the only real call site, 'kbd_ring_push(intr_dma_buffer)' is used, where 'intr_dma_buffer' is almost certainly a statically-allocated DMA buffer and therefore always valid and non-null. This makes the specific null-pointer witness a CBMC artifact rather than a realistic scenario. However, the dynamic harness DID confirm a SIGABRT — but inspection of the harness reveals the abort is caused by an *artificial* assert inserted by the harness ('assert(kbd_ring.tail != kbd_ring.head)') testing ring-overflow behaviour, not the original precondition violation on 'report'. The harness thus confirms a different potential issue (ring overflow / dropped reports) rather than the CBMC-flagged precondition on 'report'. Because the call-site always provides a valid buffer yet the dynamic result reflects a distinct artificially-injected assertion, the finding is not clearly a false positive but the specific CBMC witness is not achievable in normal execution.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
