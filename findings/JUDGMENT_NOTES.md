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

## DESIGN REVIEW (during readiness run, 2026-06-15)
1. SOUNDNESS RISK — dyn-val downgrade trusts reproducer INPUT quality.
   downgrade fires on DynamicOutcome.NOT_TRIGGERED ("harness ran clean"). Compile
   failure -> INCONCLUSIVE (NOT downgraded) = SOUND. BUT a harness that COMPILES
   but runs a NON-triggering input -> NOT_TRIGGERED -> REAL_BUG demoted. So a weak
   reproducer can demote a real bug. MITIGATION = the classifier-adjudicator
   (agents/classifier_tools.py, gated OFF) reads code to override the downgrade.
   => RECONSIDER enabling classifier-adjudicator as a downgrade GUARD (reverses the
   earlier "keep OFF" lean for that component).
2. EFFICIENCY — ~48 dynamic-harness COMPILE failures per vfs run (ld/collect2),
   each triggers an LLM repair-loop + recompile. Major latency driver. The
   deterministic harness generator has a high build-failure rate -> worth hardening
   (better link flags / type stubs) to cut repair churn.
3. CORRECTION — LLM reachability (_check_reachability_with_llm, role=realism) is
   NOT dead code: it fires when the CBMC reachability check ERRORS (observed:
   vfs_get_root, vfs_get_cwd). Earlier "dead code" claim was wrong.
4. WATCH — "falling back to unit-level harness" when system-entry harness can't
   build: realism then judges on a LESS-FAITHFUL unit harness (precision risk;
   unit harness has unconstrained inputs -> more FP-prone).

### compile-failure breakdown (root causes, prioritized)
- undefined reference to <fn>_stub : harness gen emits a stub CALL without a stub
  DEFINITION. Deterministic bug, fixable -> cuts repair churn. [HIGH value, contained]
- relocation against '<global>' in read-only .text : missing -fPIC / const-global
  handling. Fixable via build flag. [MED]
- '<var>' undeclared in _amc_setup_state : LLM-generated harness bug -> repair loop
  is the right mechanism (not deterministic). [leave to repair loop]
- undefined reference to _amc_reproducer_main : link order/visibility. [investigate]
None are soundness-critical (compile fail -> INCONCLUSIVE, no demotion); all are
EFFICIENCY/quality (high compile-fail rate -> many repair loops -> slow runs).

## readiness adjudication (cont.)
- elf: 11 confirmed (realism=realistic, several confirmed_dynamic). Adjudicated core:
  elf_validate only checks size>=sizeof(Ehdr)+magic; does NOT validate e_phoff/e_phnum/
  e_phentsize vs size. elf_calc_size loops phdr = base+e_phoff+i*e_phentsize (attacker
  offsets, no bounds) -> OOB read [REAL]; end=p_vaddr+p_memsz attacker u64 -> overflow [REAL].
  Textbook ELF-parser bugs. --agentic FOUND THEM. Strong positive readiness signal.
- klog: 0 confirmed, ran ~1min. Clean = appropriate for a trivial logging module.
- Per-module time is size-driven: klog ~1min, string ~16min+, elf ~35min (refinement loops).
READINESS so far (vfs/dtb/elf/klog): --agentic finds genuine bugs + handles clean modules right.

### harness compile-fail fix — DIAGNOSED, deferred (not safely contained)
Root cause of "undefined reference to <fn>_stub": the DYNAMIC GCC harness (not the
CBMC harness_generator) links the real source, which calls cross-module externals
(e.g. vfs.c -> fat32_file_size). Stub DEFINITIONS are emitted for direct callees but
transitive cross-module externals can be missed -> linker undefined-reference ->
compile fail -> INCONCLUSIVE -> agentic harness-repair LLM loop fixes it (observed
11x in vfs). So: SELF-HEALING via repair loop = correctness OK; cost = wasted repair
LLM calls (efficiency). Proper fix = deterministic pre-emptive stubbing of ALL
referenced externals (incl transitive/cross-module) in the GCC build, in
dynamic_validator.py build path. Non-trivial, hot-path, NOT rushed here. FOLLOW-UP.

### corroboration: compile-fail is cross-module-specific
string (self-contained utility module, no cross-module calls): 0 compile errors, 0
repairs across 19 functions. vfs (calls fat32_*): ~48 compile errors. CONFIRMS the
harness-compile problem is specifically transitive CROSS-MODULE externals. Self-
contained modules (string, klog) run clean + fast on the dynamic harness. The
follow-up fix scope is precisely: cross-module external stubbing in the GCC build.

## string adjudication — PRECISION GAP on primitives (key readiness caveat)
string: 9 confirmed (all confirmed_dynamic, realism=realistic) on memcpy/memset/
memchr/memcmp/memset32/strncpy. By MY judgment these are BORDERLINE-FP, not real:
- strncpy: realism ADMITS harness used n=2^63 ("no real caller produces") but upholds
  on GENERAL "strncpy trusts its caller". = confirming the primitive CONTRACT, not a bug.
  Dynamic "confirmation" = reproducer fed an out-of-contract n -> crash. FP pattern.
- memcpy: cited elf_load(p_vaddr=0) NULL-deref but VERIFIED chain was memmove->memcpy
  (speculative caller). Borderline low-sev at best.
=> REALISM PRECISION GAP on low-level primitives: it over-upholds contract-violations
   as "realistic" via hypothetical-caller reasoning + the reproducer confirms them by
   feeding out-of-contract inputs. This is the FLIP of the downgrade risk (here the
   reproducer CONFIRMS FPs).
READINESS PATTERN: --agentic = HIGH-VALUE on attacker-facing PARSERS (dtb/elf: real
   bugs), NOISY on utility/PRIMITIVE modules (string; likely printf/font). Full-kernel
   run should PRIORITIZE parser/attacker-surface modules; discount/skip primitive libs.

## AGENTIC-REALISM DECISION: DEFERRED (API budget exhausted) — NOT defaulted
- rt_dtb (VALID, ran before limit): agentic realism KEPT read_be64 (real OOB), correctly
  DEMOTED align4 (low-impact overflow, caller-contract) via a GROUNDING AUDIT
  ("verdict narrative-only, not source-grounded -> demote"). Promising: it reads code
  and prunes confabulated verdicts.
- rt_string (INVALID): Anthropic workspace API usage limit hit mid-run -> ALL realism
  calls (base + tool-use + spec_refiner) failed with HTTP 400 "reached workspace API
  usage limits, regain access 2026-07-01" -> SILENT FALLBACK to "upheld/confirmed_dynamic".
  The 10 "confirmed" are FAILURE ARTIFACTS, not judgments. The FP-demotion test DID NOT RUN.
- DECISION: do NOT make agentic realism default — FP-demotion claim UNVALIDATED and cannot
  be validated now (LLM budget gone until 2026-07-01). dtb signal alone is insufficient for
  a soundness-critical default flip. Re-run rt_string + cross-module validation when budget
  returns, THEN decide.
- DESIGN FINDING (confirmed materialized): realism LLM-call failure -> silent fallback to
  CONFIRMED. That means API errors/exhaustion produce FALSE CONFIRMATIONS (unsound under
  failure). Should fail-safe to UNCERTAIN/INCONCLUSIVE on realism call failure, not "upheld".
- OPERATIONAL: all --agentic LLM work (realism/spec_gen/reproducer/refiner) is BLOCKED until
  the workspace budget resets/raises (stated 2026-07-01). net/fat32 readiness also blocked.

## DESIGN FINDING: adjacent-bug pass is noise+cost on primitives
AdjacentBugAgent fires when realism REJECTS a CEx -> hunts for OTHER nearby bugs
(leads -> realism_check.adjacent_bugs[]; harvested by adjacent_follower.py). Intent:
leverage the investigation to find real bugs CBMC missed. BUT in rt2_string it produced
5-9 candidates PER trivial primitive (memcmp 9, memchr 9, strdup 8, strchr 6) -- implausible
for such tiny functions -> manufactured NOISE. Also a major latency driver (extra LLM call
per rejection + harvesting rounds). => Same parser-vs-primitive split: plausibly valuable on
complex parsers, net-NEGATIVE on primitives/utility (FP amplification + cost). RECOMMEND:
gate adjacent-bug off on primitive/utility modules, or require its candidates to clear the
agentic-realism bar before counting (else they inflate FP noise on exactly the FP-prone modules).

## EMPIRICAL: adjacent-bug pass = 130 leads, 0 confirmed bugs (net-negative as wired)
Across all judge_* --agentic runs: 130 adjacent_bugs LEADS recorded (vfs_init 36,
find_mem_child 19, print_num 12, strcat 11, ...), but 0 became confirmed bugs and 0 carry
their own verified status. Reason: leads are only HYPOTHESES in realism_check.adjacent_bugs[];
they become bugs ONLY if the separate harvesting loop (adjacent_follower.py, own CLI flag)
re-investigates them — which is NOT part of default single-pass --agentic and never ran.
=> In default --agentic the adjacent-bug pass is PURE COST (extra LLM call per rejected
finding + big latency, esp. on primitives) with ZERO realized bugs. RECOMMEND: default the
adjacent-bug pass OFF (or only enable WITH the harvester so the leads are actually verified).

## OVERNIGHT DECISION (discipline rule, realism-verdict based): PASS
realism: strncpy=unrealistic read_be64=realistic elf={'elf_validate': 'unrealistic', 'elf_calc_size': 'unrealistic', 'elf_process_relocations': 'realistic'}
reasons: strncpy=unrealistic (FP closed); read_be64=realistic (kept); elf reals realistic=['elf_process_relocations']
NOTE: read_be64 dyn-val-downgraded (reproducer not triggered) = SEPARATE soundness issue (classifier-adjudicator guard), NOT the discipline rule.
