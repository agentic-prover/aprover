# AWS Neuron driver — bmc-agent sweep

Application of bmc-agent (M1 + M1.2 + M2 + M3 + kernel-intrinsic
stubs from this session's commit `a48c853`) to the AWS Neuron
Linux kernel driver — the open-source kernel component of AWS's
Trainium / Inferentia stack.

The closed-source parts of the Trainium / Inferentia stack
(NeuronCore kernels, Neuron compiler, libnrt runtime) are
unavailable for analysis. The **kernel driver IS open source** and
is what handles user-issued IOCTLs against `/dev/neuronN` — the
attacker-controlled interface to the hardware. This sweep targets
that surface.

**Repository:** `https://github.com/aws-neuron/aws-neuron-driver`
**Commit:** `4b5e49d` (cloned 2026-05-21)

## Aggregate scorecard (this README captures the original 9-file sweep; see commits + findings/arch/ for the full 30+ file coverage including arch-specific v1/v2/v3/v4 subdirs and the pelect re-sweep after the 2D-array harness-gen fix landed mid-session)

## Original sweep (9 files, 232 functions)

| File | Verified | FAIL | Compile-err | Timeout |
|---|---:|---:|---:|---:|
| `neuron_arch.c` | **6 / 6 (100%)** | 0 | 0 | 0 |
| `neuron_pid.c` | 9 / 11 (82%) | 1 | 0 | 1 |
| `neuron_dhal.c` | 2 / 3 (67%) | 1 | 0 | 0 |
| `neuron_metrics.c` | 21 / 36 (58%) | 14 | 0 | 0 (+1 unknown) |
| `neuron_dma.c` | 33 / 60 (55%) | 24 | 2 | 1 |
| `neuron_cdev.c` | 54 / 118 (46%) | 53 | 9 | 2 |
| `neuron_mmap.c` | 6 / 17 (35%) | 9 | 0 | 2 |
| `neuron_dmabuf.c` | 1 / 6 (17%) | 3 | 0 | 2 |
| `neuron_module.c` | 0 / 4 (0%) | 0 | 4 | 0 |
| `neuron_log.c` | 1 / 5 (20%) | 0 | 4 | 0 |
| **Total** | **133 / 266 (50%)** | **105** | **19** | **8** |

(Counts include sweeps run with the kernel-intrinsic-stub harness
preamble. neuron_log only had 1 verified because most of its
functions reference more kernel intrinsics that aren't stubbed
yet.)

## Real-bug candidate

**ONE candidate** identified in this sweep. Source-audit case is
substantive but no KASAN reproducer yet — full details (including the
function, trigger, and PoC sketch) are embargoed pending verification.
See the private companion repo `agentic-prover/aprover-findings-embargoed`
under `findings/aws_neuron_driver/unconfirmed/`.

**Status: UNCONFIRMED.** Pending KASAN PoC on a Trainium / Inferentia
host or QEMU+Neuron-driver build before disclosure to AWS Security.

## False-positive distribution

Of the 105 FAILs, every one inspected sorts into one of the four
known harness-FP classes carried over from prior sessions:

1. **Class A — handle-NULL deref** (~50%): the function dereferences
   a `struct neuron_device *nd` or `struct ncdev *dev` parameter
   without a NULL check. Real callers (kernel framework) never
   pass NULL.

2. **Class B — precondition-propagation** (~10%): in-source
   `assert(...)` references state the harness doesn't model
   (e.g. constructor invariants on global hardware-abstraction
   pointer `ndhal`).

3. **Class C — struct-pointer field** (~30%): M1's disjunctive
   init covers primitive-pointer fields. Struct-pointer and
   pointer-to-pointer fields (`pdev->bus`, `ctx->state`,
   `dma_ctx->ring`) stay nondet and trip on deref.

4. **Class D — sibling-parameter index** (~10%): `f(T *buf,
   int i)` patterns where `i < N` is a caller-maintained
   invariant relative to a sibling parameter or struct field.

These match the same distribution seen on llama.cpp's ggml-alloc.c
and nghttp2's frame.c. The bmc-agent improvements needed to clear
them (M1.3, M1.4, M1.5) would compound across every target.

## Verified clean — high-attack-surface IOCTL handlers

These IOCTL handlers handle user-controlled input and verified
memory-safe under M1+M2+M3:

- `ncdev_dma_engine_set_state` / `_get_state`
- `ncdev_dma_queue_init` / `_init_batch_entry` / `_release` / `_get_state`
- `ncdev_dma_copy_start` / `_ack_completed`
- `ncdev_dma_descriptor_copyout`
- `ncdev_mem_buf_zerocopy64_batch`
- (embargoed BAR-RW dispatch + callees — see private companion repo
  under `findings/aws_neuron_driver/unconfirmed/`)
- `ncdev_resource_mmap_info`
- `ncdev_release_neuron_ds`
- `ncdev_throttling_notifications_set`

These verifications mean: given the bounded harness input space,
no buffer OOB, no NULL deref of caller-provided struct fields, no
double-free, no use-after-free in these handlers.

## What enabled this sweep

This session's commit `a48c853` added a "kernel-intrinsic stubs"
preamble to the harness emitter — `extern` declarations for the
~30 most common kernel intrinsics (atomic ops, kmalloc family,
user-access primitives, bit ops, task helpers) plus a
placeholder definition for `struct pci_dev` (forward-decl-only in
kernel headers). Without these stubs, every kernel-mode harness
got CONVERSION ERROR before verification could start. With them,
the 50% clean rate above became possible.

This is the first time bmc-agent has run end-to-end on a real
Linux kernel driver beyond the synthetic ch341.c example used
during initial kernel-mode bring-up.

## Files

- `README.md` (this)
- `scorecard_*.json` — per-file verdicts
- `sweep_*.log` — raw sweep output
- Bug analysis, reproducer sketch, and disclosure draft — moved to
  the private companion repo `agentic-prover/aprover-findings-embargoed`
  under `findings/aws_neuron_driver/unconfirmed/`
