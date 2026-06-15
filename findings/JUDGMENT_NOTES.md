# My-judgment adjudication of VibeOS vfs findings (no oracle; reading the code)

Source: examples/vibeos/repo/kernel/vfs.c

- vfs_readdir (L422): in-mem path bounds-checks index, but
  `strncpy(name, child->name, name_size - 1)` with name_size a size_t — caller
  passing name_size==0 underflows to SIZE_MAX => unbounded OOB write into name.
  JUDGMENT: PLAUSIBLE REAL BUG (zero-size-buffer underflow). Should be KEPT.

- vfs_write (L601): `new_cap = size + 64; malloc(new_cap); memcpy(file->data,
  buf, size)`. Integer overflow only at size ~ SIZE_MAX, which needs an
  impossibly large buf. JUDGMENT: NOT a realistic bug. Demoting is CORRECT.

- vfs_append (L632): `malloc(file_size + size)` same overflow class; needs
  impossible size. JUDGMENT: NOT realistic (FP). Demoting is CORRECT.

## Implication for arms (re-scored by MY judgment, not the gate labels)
- flat (baseline/opusjudge/haikumech): KEEP readdir, drop write+append =>
  CORRECT on all three by my judgment. (The gate called this RED only because
  its vfs_write label is wrong.)
- claude-code all-agentic (ccall2): DROP readdir (misses the real bug) + KEEP
  write (upholds an unrealistic overflow) => 2 judgment ERRORS.
- in-process tools (mode2): DROP readdir + KEEP append (FP surfaced) =>
  2 judgment ERRORS (worst).
- judge_default (in-process tools, fresh): upheld vfs_write (lenient) — and
  mode2 dropped it — => NONDETERMINISTIC on borderline cases.

## Working conclusion (to confirm with flat-vs-tools fresh comparison)
Flat agents produce the most defensible findings here; the tool-using
spec_gen/reproducer reshape specs/harnesses and add judgment errors + variance
on borderline overflow cases. Reasonable default leans FLAT (esp. keep realism
flat — already is — and consider --no-spec-gen-tools).

## TASK SCOPING (user, 2026-06-15)
Deliverable = a DEFAULT plan for --agentic: per COMPONENT, flat (mode1) vs
in-process tool-using agent (mode2). INDEPENDENT of the claude-code "all"
choice (mode3 / --agentic-claude-code) — that is a separate orthogonal switch,
set aside (also ~$12/sweep, not the default-plan question).

Components with a real flat-vs-agent choice (have *_tools variants):
  spec_gen, bmc_config(cbmc_driver), reproducer(dynamic_repro), triage, harness_gen.
Flat-by-nature (no tool variant): refinement, feedback_distill, classifier,
  disagreement_diagnose. realism_tools is force-OFF under --agentic already.

METHOD: after the all-tools-on default sweep, run PER-COMPONENT ablations on the
module(s) where the default surfaced FPs / missed reals — toggle each tool agent
individually (--no-spec-gen-tools | --no-reproducer-agent | --no-bmc-config-agent)
and judge (by reading code) whether that component's tool-use ADDS real bugs or
INTRODUCES FPs/variance. Attribute flat-vs-agent per component => default plan.

## classifier vs realism — traced to ground (2026-06-15)
NOT redundant; a clean producer->consumer pipeline:
- validate() [classifier]: ONE role=realism LLM call = REACHABILITY -> builds caller_path/system_entry.
- realism.check(): consumes caller_path DETERMINISTICALLY (_format_caller_context /
  _format_call_site_analysis), makes ONE distinct role=realism LLM call = EXPLOITABILITY.
Only shared thing = the model routing role (intentional). No duplicated LLM call to remove.
Full merge = relocate reachability tracer into realism (big, risky, ~0 net simplification) -> declined.
DECISION: leave classifier/realism as-is.

## dtb ablation — adjudicated (2026-06-15) — REVERSES the vfs-based lean
deftools (all tools) vs flat (--no-spec-gen-tools --no-reproducer-agent --no-bmc-config-agent), same dtb module:
- deftools: 6 CEx -> 2 CONFIRMED real bugs (by my reading):
  * align4.overflow.1  [confirmed_dynamic, realistic]: (offset+3)&~3 overflows; attacker `len`
    from FDT_PROP read via read_be32 with NO bounds check -> offset+len ~UINT32_MAX. REAL.
  * read_be64.pointer_dereference.35 [confirmed_system_entry]: reads 8 bytes unconditionally on
    'reg' prop bounded by attacker len, NO len>=8 check -> OOB read. REAL.
- flat: 6 CEx -> 0 confirmed (4 unresolved). MISSED both real bugs.
=> On a real parser, the TOOL config found real bugs flat missed. OPPOSITE of the vfs lean
   ("tools hurt"). Per-module variance is high; vfs conclusion does NOT generalize.
ATTRIBUTION PENDING: reproonly arm (only reproducer agentic) isolates whether the reproducer
   is the driver vs spec_gen/bmc_config tools.
NOTE: latency myth busted — deftools ~24min ~= flat ~23min on dtb. Earlier "tools 2-3x slower"
   was module-size confounded (vfs 24KB/92min vs dtb 6.5KB). CBMC+realism dominate, not tools.
