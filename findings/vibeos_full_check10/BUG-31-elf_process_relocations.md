# BUG-31 — `elf_process_relocations` (elf)

| Field | Value |
|---|---|
| **Confidence** | `confirmed_bmc` |
| **Signal** | — |
| **Module** | `kernel/elf.c` |
| **Realism** | realistic |
| **Status** | ☐ Unreviewed |

## Call chain

System entry point (no upstream callers traced)

## Spec (LLM-generated)

**Precondition:** `requires load_base != 0 && valid(dynamic) && the memory region starting at dynamic is a valid null-terminated array of Elf64_Dyn entries (terminated by an entry with d_tag == 0) && if any entry has d_tag == 7 (DT_RELA), then (load_base + d_val) is a valid, in-bounds pointer to an array of Elf64_Rela entries within the loaded ELF image && if any entry has d_tag == 8 (DT_RELASZ), then rela_size does not overflow when added to load_base + rela_addr && if any entry has d_tag == 9 (DT_RELAENT), then d_val > 0 and d_val >= sizeof(Elf64_Rela) to prevent division producing an inflated count && for each RELATIVE relocation entry (r_info & 0xFFFFFFFF == 0x403), (load_base + r_offset) is a valid aligned uint64_t* within the loaded image's writable memory && (load_base + r_addend) does not overflow uint64_t && all PT_LOAD segments of the ELF have been mapped into memory at addresses relative to load_base && the ELF has been validated (e_type == ET_DYN == 3, e_machine == EM_AARCH64 == 183) prior to this call`

**Postcondition:** `ensures all DT_RELA relocations of type R_AARCH64_RELATIVE (0x403) described in the dynamic section have been applied to the loaded ELF image in memory: for each such relocation entry with offset o and addend a, the uint64_t at address (load_base + o) has been set to (load_base + a) && no writes occur outside the bounds of the loaded ELF image's writable segments && if rela_addr == 0 or rela_size == 0 the function returns without modifying memory && unknown relocation types are logged but do not cause memory corruption or undefined behaviour && the function does not return a value; callers rely solely on the side effect that all absolute addresses and GOT entries requiring RELATIVE fixups are correctly patched so the PIE image is ready for execution at load_base`

## Counterexample

**Violated property:** `elf_process_relocations.unwind.0`

**Key variable assignments:**
```
load_base = 73183493944770560ul
_dynamic_val = <symbolic struct/array — see classification.json>
dynamic = _dynamic_val!0@1
rela_addr = 0ul
rela_size = 11176474785116848128ul
rela_ent = 94567198519296ul
dyn = <symbolic struct/array — see classification.json>
```

## Root cause

CBMC reports a `elf_process_relocations.unwind.0` failure — a semantic / contract violation in `elf_process_relocations`.

**Validator reasoning:** Refinement was over-restrictive at iteration 1 — would exclude states that callers can actually produce. Treating as real bug to be safe.

## How to trigger

`elf_process_relocations` is reachable as a system-entry point — call it directly with the counterexample's variable assignments.

## Realism assessment

**Verdict:** REALISTIC (high confidence)

Q1 — Can the violation TYPE occur? YES. The loop `for (const Elf64_Dyn *dyn = dynamic; dyn->d_tag != 0; dyn++)` has no bounds check and relies solely on finding a DT_NULL terminator (d_tag == 0) to stop. In the ELF format, the dynamic array MUST be null-terminated, but this is a structural property of the input data, not enforced by the code. An attacker can craft a malicious ELF binary whose `.dynamic` section lacks a DT_NULL terminator. Without any length limit or valid-range check, the loop would walk off the end of the dynamic array into unmapped or attacker-controlled memory, causing an out-of-bounds read. This is a well-known ELF loader attack vector. Q2 — Is the witness realistic? The counterexample shows a single dynamic entry with d_tag=7 and no terminating entry with d_tag=0. This directly models a truncated or maliciously crafted dynamic section — a realistic input from an attacker-supplied ELF binary. The call site (`elf_load_at`) processes ELF binaries that could be untrusted. The loop-unwind violation (.unwind.0) here corresponds to a real non-termination / out-of-bounds scenario, not merely a CBMC bound artifact. Since the function operates as an ELF loader and parses data from external binaries with no bounds enforcement, both the violation type and witness scenario are plausible in a real attacker-controlled input setting.

## Manual review checklist

- [ ] Confirm the call chain is reachable in the actual VibeOS codebase
- [ ] Verify the counterexample variable assignments are achievable at runtime
- [ ] Check whether a fix is already present in a newer version
- [ ] Assess exploitability severity (crash-only / memory corruption / arbitrary write)
- [ ] File upstream issue if confirmed
