# Trust-boundary note — VibeOS kernel

VibeOS is a bare-metal ARM64 hobby kernel (~15k LOC, substantial LLM authorship).
There is no userspace/kernel privilege split to lean on: the attacker model is
**untrusted data that crosses into the kernel from outside the running image.**

## Attacker surface (attacker-controlled)

- **Loaded images / filesystems:** any bytes parsed out of a disk image, an ELF
  the kernel loads/executes, or a device tree blob (DTB) the firmware hands in.
  All header fields, sizes, counts, offsets, and string lengths read from these
  are fully attacker-chosen — including values that index arrays, drive loop
  bounds, or feed allocation sizes.
- **Syscall / trap arguments:** any pointer, length, or index a user program
  passes through the syscall/trap path. Treat user-supplied lengths and indices
  as unbounded and user pointers as possibly-invalid.
- **MMIO / device input:** bytes read back from device registers and DMA
  buffers (e.g. console/UART input, block-device reads) are not trusted.

For any value derived (even after arithmetic) from the above, assume the
attacker controls it and that intermediate computations may overflow.

## Trusted inputs (NOT attacker-controlled) — conservative

- Pointers to kernel objects that a caller allocates and fully initializes
  before any attacker data is processed (e.g. a `task`/`vfs_node` constructed by
  kernel init) → non-NULL and structurally valid, *unless a field of that object
  was itself populated from attacker data*, which remains untrusted.
- `static const` tables compiled into the image → valid for their fixed extent.

Everything not in this list defaults to attacker-controlled. Do **not** add a
precondition that bounds an attacker-controlled size/index/length — if a check
for it is missing in the code, that absence is the bug.

## Properties that count

Memory safety first: out-of-bounds read/write, use-after-free, NULL/invalid
deref, and integer overflow that feeds an allocation or a bound. A panic/assert
reachable from attacker-controlled input also counts (DoS of a bare-metal OS).
