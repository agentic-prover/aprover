# Trust-boundary note — TEMPLATE

Copy this file, fill it in for your target, and pass it with
`--threat-model-context path/to/your_note.md`.

It is injected verbatim into every trust-deciding role (spec generation,
refinement, classifier, dynamic validation, realism) so the **precondition is
shaped correctly at generation time** instead of being patched post-hoc by the
realism filter. A precondition is, literally, a statement of what a function
trusts about its inputs — so this note is precondition-shaping information.

## The one rule that makes this safe

**Be conservative. List something as TRUSTED only when a caller or hardware
*provably* guarantees it.** A too-generous "trusted" list masks the very bugs
we are looking for — and it masks them at generation time, before any gate can
catch them. When unsure, leave it OUT of the trusted list; the pipeline already
defaults every unlisted input to attacker-controlled.

Keep it prose. There is no schema to satisfy — the model reads it as context.

---

## Attacker surface (what is attacker-controlled)

Describe the inputs an attacker influences and how they reach the code. Examples:

- **Entry points:** e.g. the fuzz harness `LLVMFuzzerTestOneInput(data, size)`;
  the syscall dispatch table; an ELF/firmware/disk image loaded from untrusted
  media; network packets at `recv()`.
- **Attacker-controlled fields:** which parameters, struct fields, and global
  buffers carry attacker bytes. Be specific: `len` in `parse(buf, len)` is
  fully attacker-chosen; `hdr->n_entries` is read directly from the file.
- **Reach:** the call path from an entry point to the functions under analysis,
  so reachability reasoning has real targets to trace toward.

## Trusted inputs (what is NOT attacker-controlled) — be conservative

Only list inputs with a *provable* guarantee, and state the guarantor:

- e.g. `ctx` is allocated and fully initialized by `init_ctx()` before any
  attacker data is processed → non-NULL, fields valid.
- e.g. `table` points to a `static const` array of fixed size N → always valid
  for indices `0..N-1`.

## Properties that count as real bugs

Optionally narrow what matters for this target (e.g. OOB read/write,
use-after-free, integer overflow feeding an allocation). Leave blank to use the
`--threat-model` mode default.
