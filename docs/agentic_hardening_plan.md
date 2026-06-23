# --agentic hardening — plan & resume state

Resume anchor for the `--agentic` work. Branch: **`reproducer-agent-merge`**
(NOT pushed, NOT merged to main — this repo works linearly on main).

## HOW TO RESUME (two modes)

### A. Interactive, with permissions bypassed
Launch Claude with bypass (no prompts), then point it here:
```
claude --dangerously-skip-permissions
```
Prompt: "Resume the AProver --agentic hardening. SSH to syc@135.181.215.190,
cd ~/AProver, git checkout reproducer-agent-merge, read
docs/agentic_hardening_plan.md, and continue the budget-free track autonomously
(start by wiring agent_registry.py). Validate every change against the
54-failure baseline AND run test_phase3.py in isolation."

### B. Unattended overnight (headless, on the box)
```
nohup ~/AProver/tools/overnight_agentic_hardening.sh > /tmp/overnight_hardening.out 2>&1 &
```
Loops headless `claude` on the box: each iteration does the single next
incomplete budget-free step, validates, commits; reverts+stops on any new
failure; stops on BUDGET_FREE_TRACK_COMPLETE or an 8-iter cap. Never runs the
budget-gated track. Watch: `tail -f /tmp/overnight_hardening.out`; per-iter logs
in `findings/overnight_hardening/`. Stop: `rm -f /tmp/agentic_hardening.lock &&
pkill -f overnight_agentic_hardening`.

## Done this session (committed on the branch)

```
7193ae6 tools: unattended overnight runner for the budget-free hardening track
6d655bf wip: agent-registry draft (not yet wired) + hardening plan/resume doc
5f2700b docs: agent telemetry + soundness gate usage and next steps
41dbcc8 soundness: standing tiering-logic guard + adjudication checker
b46fd28 telemetry: per-agent runtime instrumentation
3cfb465 fix: guard reproducer-agent output against non-str leaking into dyn-val
8f5cfa6 agentic harness: default-ON repair + AgenticHarnessGen as harness_gen BaseAgent
295451a pipeline: add off-switch for Phase 3d oracle-disagreement diagnosis (default OFF)
ed6616a agents: merge DynamicReproAgent into the tool-using ReproducerAgent
feadb99 bmc-config + reproducer agents: default ON with --no- toggles
f885ee9 realism-enforcement: Phase 3 DONE — GATE GREEN, enforcement stays default-ON
```

Current default-on `--agentic` agents (10): spec_gen (+ split pass-2), bmc_config
(cbmc_driver), classifier, refinement, soundness (rides refinement), feedback_distill,
realism, reproducer (dynamic_repro), harness_gen. OFF by default: disagreement_diagnose,
dynval_triage, realism-tools, triage, agentic-harness-primary.

## Validation discipline (use for EVERY change)
- Full suite: `python3 -m pytest tests/ -q -p no:cacheprovider` — baseline is **54 failures**
  (all pre-existing/unrelated: rust-parser ModuleNotFoundError, cache_prefix threat-model
  drift, phase/kani env). A change is clean iff it adds ZERO new failures (count stays 54).
- ALSO run `tests/test_phase3.py` ALONE — full-suite ordering masks regressions (this is how
  the reproducer non-str leak slipped through once). Isolated baseline = 3 failures.

## Plan (re-sequenced: budget-free first, since the live sweep is on hold)

### Budget-free track — NO LLM/CBMC, do autonomously (this is what the overnight runner does)
1. **Agent registry (IN PROGRESS).** DRAFT MODULE COMMITTED: `bmc_agent/agent_registry.py`
   (AGENT_ROLES + REGISTRY + label_for, 11 roles). NOT yet wired — nothing imports it. Remaining:
   - `config.py`: replace the literal role tuple in the env-routing loop
     (`for role in ( "spec_gen", ... "harness_gen" ):`) with `for role in AGENT_ROLES:`,
     add `from bmc_agent.agent_registry import AGENT_ROLES`.
   - `cli.py`: replace the `ALL_AGENT_ROLES = ( ... )` literal in `_apply_provider_args`
     with `ALL_AGENT_ROLES = AGENT_ROLES` (import at top).
   - Add `tests/test_agent_registry.py` pinning AGENT_ROLES to the exact historical 11-role
     set {spec_gen, feedback_distill, refinement, realism, classifier, disagreement_diagnose,
     triage, dynamic_repro, dynval_triage, cbmc_driver, harness_gen} so accidental drift fails.
   - Optionally fold the AI-layers printout labels onto `label_for` (lower priority).
2. **Token plumbing into telemetry.** Thread `usage` (prompt/completion tokens) out of
   `LLMClient.complete()` / `complete_with_tools()` (llm.py logs it at ~117/389 but doesn't
   return it) into `agent_telemetry` (the `tokens` field is reserved, currently 0). Turns
   duration into $/finding. Deterministic; testable with mocks.
3. **Centralize output-contract validation in `BaseAgent`.** Make `run()`/`parse` enforce the
   declared output type and return None/error on violation, so the non-str-leak class (the
   reproducer regression, fixed reactively in 3cfb465) can't recur.
4. **Test fidelity.** Add randomized test order (pytest-randomly) in CI + faithful agent
   test-doubles, so a default-flip can't silently regress behind ordering.

### Budget-gated track — needs a live --agentic sweep (ON HOLD pending user go)
0. **Live baseline (Phase 0).** `verify-dir --agentic` on a known-oracle fixture (recommend
   VibeOS vfs — reals vfs_readdir/vfs_write, FPs vfs_append/vfs_delete_recursive). Capture
   `<artifact_dir>/agent_telemetry.json` + run
   `tools/check_soundness_gate.py <findings_dir> --reals vfs_readdir,vfs_write --fps vfs_append,vfs_delete_recursive`.
   Exit: a $/role baseline + GREEN gate.
5. **Measured efficiency (Phase 2).** One change at a time: route low-judgment/high-volume
   roles to a cheaper model via `BMC_AGENT_LLM_<ROLE>_PROVIDER` (no code change), and flatten
   tool-loop agents where tools don't move recall/precision. After each: re-sweep, diff
   telemetry (cost) + soundness gate (recall GREEN) + FP rate. Keep wins, revert duds.
   (flat-vs-agentic and cheap-vs-expensive are ORTHOGONAL knobs.)

### Land it (Phase 4)
Run the empirical gate once more, decide branch/merge strategy, land on main.

## Loose ends
- Branch `reproducer-agent-merge` is unpushed / unmerged.
- Repo `git gc` / "too many unreachable loose objects" warning — a one-time `git gc` clears it.

## Tooling reference
- Telemetry: `bmc_agent/agent_telemetry.py`; per-run dump at `<artifact_dir>/agent_telemetry.json`.
- Soundness: `tests/test_soundness_corpus.py` (deterministic), `tools/check_soundness_gate.py`
  (empirical, over a real findings dir). See `docs/agent_telemetry_and_soundness.md`.
- Overnight runner: `tools/overnight_agentic_hardening.sh` (mode B above).

---

## SESSION 2026-06-14 (budget-free track + tuning kickoff)

### Committed this session (branch reproducer-agent-merge)
- `b86b655` registry: wire AGENT_ROLES into config+cli + pin test (step 1 DONE)
- `2a9efb7` telemetry: plumb token usage LLMClient -> agent_telemetry (step 2 DONE)
- `88c84af` llm: omit temperature for claude-opus-4-8 (it 400s on temperature);
  unblocks per-role Opus routing. Tests: test_llm_temperature_guard.py.
All validated: full suite 54 (== baseline), test_phase3.py isolated 3 (== baseline).

### PIVOTAL FINDING — "agentic vs flat" is a BACKEND question, not per-agent
The box env (~/.config/bmc-agent/env) is **anthropic-only**, default model
`claude-sonnet-4-6`, no per-role overrides. Under plain `--agentic`:
- `LLMClient.complete_with_tools` RAISES NotImplementedError for provider=anthropic
  ("requires the openai-compatible provider"). So the tool-using agents
  (spec_gen-tools, bmc_config, reproducer, harness_gen) ERROR on the tool path
  and silently fall back (e.g. "bmc config for X produced no output"). 28x in the
  baseline log. => Tool agents are NOT actually agentic on this deployment today.
- `--agentic` also FORCE-DISABLES realism tools (log: "AI layers OFF: ... realism
  tools"), despite enable_realism_tools defaulting True. So RealismToolsAgent is
  OFF under --agentic (corrects the earlier inventory).
- The only genuinely-working agents under plain --agentic are the FLAT ones:
  realism (Pass-1), refinement, feedback_distill.

### The unblock (no code change): claude-code backend
`claude` CLI is installed (/usr/local/bin/claude v1.0.110). Codebase has a
`claude-code` provider (shells to `claude -p` with read-only Read/Grep/Glob).
`_agent_runs_on_claude_code()` = True when claude_code_agentic AND provider==claude-code.
`--agentic-claude-code` forces EVERY role onto claude-code => genuinely agentic.
THIS is how "make all agents agentic" is done here — a flag.

### Tonight experiment matrix (all flag/env, NO hot-path code)
- Arm A baseline: `--agentic` (anthropic sonnet; tool agents degraded). RUNNING.
- Arm B all-agentic: `--agentic-claude-code` (every agent investigates via CLI).
- Arm C opus-judgment: `--agentic` + BMC_AGENT_LLM_{REALISM,REFINEMENT}_MODEL=claude-opus-4-8.
- (Arm D haiku-mechanical: feedback_distill -> claude-haiku-4-5, if time.)
Each: capture <root>/agent_telemetry.json (now incl. tokens) + check_soundness_gate.py
(reals vfs_readdir,vfs_write; fps vfs_append,vfs_delete_recursive). Keep GREEN wins.
Runner: tools/tune_agentic.sh LABEL (env: AGENTIC_FLAG, PER_FUNC_BUDGET, EXTRA_FLAGS,
per-role BMC_AGENT_LLM_*). Fixture: examples/vibeos/repo/kernel/vfs.c driver vibeos_vfs.

### UPDATE — original ccall (all-agentic) result was INVALID; claude-code was broken
User intuition ("something wrong when all agents used?") was right. The
--agentic-claude-code arm dropped 2 reals — but NOT because agentic is worse:
every `claude -p` call exited 1 on flags the installed claude CLI v1.0.110
rejects, so agents fell back to seed-only (66 fallbacks). Fixed in `d47f50c`:
  - --permission-mode dontAsk -> bypassPermissions
  - dropped --no-session-persistence (unknown)
  - --system-prompt -> --append-system-prompt
  - text-only --tools "" -> --disallowed-tools <list>
claude-code now completes (returns content; cost_usd ~0.06/call, ~16k cache-
creation overhead per call => all-agentic sweeps are $$$). Re-running as
arm ccall2 via tools/tune_rerun.sh. DISCARD findings/tune_ccall_2026* (broken).

### Arm results so far (gate: reals vfs_readdir+vfs_write must stay; FPs vfs_append+vfs_delete_recursive must demote)
- baseline (all sonnet, flat; tool agents degraded): RED — keeps readdir, DROPS write.
- opusjudge (realism+refinement -> opus): RED — same as baseline (model strength not the lever).
- ccall (all-agentic): INVALID (claude-code broken) — re-running as ccall2.
- haikumech (feedback_distill -> haiku): running.
Open thread: vfs_write demoted by every valid config so far => likely a
refinement/demotion-logic issue, NOT a model/agentic-routing one.

### UPDATE 2 — anthropic-native tool use implemented (commit 4fedd69); ccall2 result
Per user direction, implemented _anthropic_tool_use_loop so complete_with_tools
works on anthropic (was openai-only). Unlocks MODE 2 (in-process *_tools.py
agentic variants) on the anthropic-only box without an external endpoint.

THREE agentic modes per agent now distinguishable:
  mode 1 flat (complete) | mode 2 in-process tool loop (*_tools via complete_with_tools)
  | mode 3 claude-code CLI (--agentic-claude-code).

Arm results (gate: keep reals vfs_readdir+vfs_write; demote FPs vfs_append+vfs_delete_recursive):
| arm        | config                          | readdir | write | FPs  | gate | cost   |
| baseline   | sonnet flat, tools DEGRADED     | KEEP    | DROP  | both | RED  | ~$0    |
| opusjudge  | realism+refine -> opus (flat)   | KEEP    | DROP  | both | RED  | ~$0    |
| haikumech  | feedback_distill -> haiku       | KEEP    | DROP  | both | RED  | ~$0    |
| ccall2     | all-agentic claude-code (mode3) | DROP    | KEEP  | both | RED  | ~$12   |
NB ccall2 killed after oracle funcs settled (saved budget); oracle verdicts genuine
(vfs_readdir processed @ log:624, demoted; vfs_write upheld confirmed_dynamic).
Striking: flat arms keep readdir/drop write; all-agentic keeps write/drops readdir.
No arm GREEN yet. mode2 (in-process tools on anthropic, post-fix) = running now.

---

## FINAL RESULTS — agentic-vs-flat / model tuning (5 arms, VibeOS vfs fixture)

Gate = keep reals {vfs_readdir, vfs_write}; demote FPs {vfs_append, vfs_delete_recursive}.

| arm       | agentic mode            | model(s)              | readdir | write | append(FP) | del_rec(FP) | gate | cost   |
|-----------|-------------------------|-----------------------|---------|-------|------------|-------------|------|--------|
| baseline  | flat (mode1)            | sonnet-4-6 (all)      | KEEP ✓  | DROP ✗| demoted ✓  | demoted ✓   | RED  | ~$0    |
| opusjudge | flat (mode1)            | opus realism+refine   | KEEP ✓  | DROP ✗| demoted ✓  | demoted ✓   | RED  | ~$0    |
| haikumech | flat (mode1)            | haiku feedback_distill| KEEP ✓  | DROP ✗| demoted ✓  | demoted ✓   | RED  | ~$0    |
| ccall2    | claude-code (mode3)     | claude CLI (all)      | DROP ✗  | KEEP ✓| demoted ✓  | demoted ✓   | RED  | ~$12   |
| mode2     | in-process tools (mode2)| sonnet-4-6 (all)      | DROP ✗  | DROP ✗| KEPT ✗(FP) | demoted ✓   | RED  | ~$0    |

### Conclusion (answers: which agent agentic vs flat? which model?)
1. **No arm passed the gate.** Every configuration drops at least one real bug.
2. **More-agentic did NOT help — it hurt.** Flat keeps readdir/drops write (1 real lost);
   claude-code keeps write/drops readdir (1 real lost, ~$12/sweep); in-process tools is
   WORST — drops BOTH reals and surfaces a false positive (vfs_append confirmed). Turning the
   in-process *_tools agents on reshaped specs/harness/repro such that realism downgraded both
   reals and "confirmed" a FP.
3. **Model choice was gate-neutral.** opus on realism+refinement and haiku on feedback_distill
   changed nothing vs the sonnet baseline. No evidence to justify routing any role to opus;
   haiku on feedback_distill is a safe (gate-neutral) cost save if desired.
4. **The real blocker is a pipeline issue, not routing.** vfs_write is demoted by a
   classifier/realism stage downgrade in EVERY flat arm (model- and agentic-invariant). The
   agentic-vs-flat and model knobs cannot fix it.

### Recommendation
- KEEP AGENTS FLAT (mode 1). Do not adopt all-agentic: no recall benefit, higher cost
  (claude-code ~$12/sweep), higher variance, and in-process tools regress soundness.
- KEEP default model (sonnet-4-6) for all roles; optionally haiku for feedback_distill (cost,
  gate-neutral). Revisit per-role opus only AFTER the gate is GREEN.
- FIX THE CLASSIFIER/REALISM DEMOTION of vfs_write FIRST (root cause), then RE-RUN this
  experiment — only then will agentic-vs-flat / model signal be readable above the noise.
- CAVEAT: single fixture (vfs), single gate. ccall2 + mode2 truncated after oracle functions
  settled (oracle verdicts authoritative; gate run on finalized findings).

### Hardening commits landed this session (branch reproducer-agent-merge)
b86b655 registry wiring + pin | 2a9efb7 token telemetry | 88c84af opus temperature fix
d47f50c claude-code CLI v1.0.110 flag fix | 4fedd69 anthropic-native complete_with_tools
All validated: full suite 54 (== baseline), test_phase3.py isolated 3.

---

## PER-COMPONENT --agentic DEFAULT PLAN (judgment-based; vfs + dtb evidence)

Method: ran ablations, adjudicated every confirmed finding by READING the code
(my judgment = oracle). dtb ablation (deftools/flat/reproonly) was decisive.
Excludes the claude-code "all" mode (separate orthogonal switch).

### Evidence summary (dtb, all findings adjudicated by me)
- read_be64 OOB read (REAL): caught by flat, deftools, AND reproonly -> the
  always-on validator+realism backbone catches the clear real bug; tools not needed.
- align4 unsigned overflow (REAL): caught ONLY when the reproducer is on -> the
  reproducer adds recall.
- dtb_parse NULL-deref (BORDERLINE / not attacker-reachable under security TM):
  suppressed by spec_gen/bmc_config tools (reasonable dtb_addr!=NULL precondition)
  -> tools add PRECISION, did not lose a real bug.
- Latency: deftools ~= flat (~24min on dtb). The "tools 2-3x slower" was module-size
  confounded (vfs). CBMC+realism dominate runtime, not the tool agents.

### Decision per component (flat vs in-process agent)
| component        | default        | confidence | rationale (by my judgment)                                   |
| validation(CBMC) | ALWAYS-ON      | high       | reachability+feasibility backbone; catches read_be64 regardless. (done: cfbe562) |
| realism          | FLAT, always-on| high       | sound, code-grounded FP filter; its dtb reasoning matched my own reading. |
| reproducer       | AGENT          | med-high   | adds real-bug recall (align4) + dynamic confirmation; investigation->triggering input. |
| spec_gen         | AGENT          | medium     | improved precision on dtb (suppressed borderline NULL-deref via sound precondition); no real-bug loss. |
| bmc_config       | AGENT          | low-med    | mechanical flags/inline; bundled w/ spec_gen; neutral-to-positive, keep. |
| harness_gen      | AGENT          | low        | native anthropic tool loop; fires only on build error; low blast radius. |
| refinement       | FLAT (variant OFF) | medium | new tool variant built but no recall benefit shown; adds latency/variance -> keep OFF. |
| feedback_distill | FLAT (variant OFF) | medium | transformation task; no demonstrated benefit -> keep OFF. |
| classifier-adjud | OFF            | medium     | adjudicator at downgrade point; only enable if a real bug is being lost to dyn-val downgrade. |
| triage           | OFF (current)  | low        | not exercised; leave default. |

### Bottom line
The CURRENT --agentic default (reproducer + spec_gen + bmc_config tools on;
refinement/feedback/classifier-tools off) is broadly REASONABLE on this evidence.
The substantive corrections this session: (1) make CEx validation always-on
(done), (2) keep the new in-process variants OFF by default until one shows a
recall win, (3) the reproducer is the highest-value agentic component (protect it).
CAVEAT: 2 modules, borderline findings; confirm on elf/net before hard-coding.

---

## FULL-KERNEL READINESS VERDICT (judgment-based; 5 modules adjudicated)

Per-module results (every confirmed finding adjudicated by reading the code; NO oracle):

| module | KB | type            | ran clean | confirmed | my verdict                          | runtime |
|--------|----|-----------------|-----------|-----------|-------------------------------------|---------|
| vfs    | 24 | syscall surface | yes       | ~1+demotes| REAL (vfs_readdir OOB); unreal overflows correctly demoted | ~92m |
| dtb    | 6.5| parser          | yes       | 2         | BOTH REAL (read_be64 OOB, align4 ovf); NULL-deref correctly suppressed | ~24m |
| elf    | 8  | parser          | yes       | 11        | core REAL (header-bounds OOB + overflow; classic ELF vulns) | ~35m |
| klog   | 1.8| utility         | yes       | 0         | correctly CLEAN                     | ~1m  |
| string | 7  | primitives      | yes       | 9         | BORDERLINE-FP (primitive contract-violations upheld on hypothetical callers; harness used n=2^63) | ~50m |
(printf/memory running/queued; net/fat32 = the high-value big parsers, pending.)

### VERDICT
- **Per-module lead-generation on the ~9 driver-ready modules: GO.** Ran clean on all 5
  (no crashes, no all-fallback, no NotImplementedError post-fixes). Found GENUINE bugs on
  attacker-facing parsers (dtb/elf/vfs) and was correctly clean on klog.
- **Entire 27-module kernel, turn-key: NO-GO.** Blockers:
  (a) ~18 modules lack self-contained drivers (console/font/irq/keyboard/process/tls/ttf/
      virtio_*/...) -> CBMC can't compile them as-is; need driver scaffolding or --standalone
      validation. THE hard blocker.
  (b) Runtime is size-driven and large (1m..92m/module); whole kernel = a MULTI-NIGHT batched
      sweep with per-module timeouts, not one command.
  (c) Output needs TRIAGE: high-signal on parsers, NOISY on primitives (string FP cluster).
  (d) Design caveats (recorded): downgrade trusts reproducer input quality (soundness risk;
      classifier-adjudicator is the guard); realism PRECISION GAP on low-level primitives
      (over-upholds contract-violations); cross-module dynamic-harness compile-fail churn.

### RECOMMENDATION
1. NOW: run --agentic on driver-ready PARSER/attacker-surface modules (dtb, elf, net, fat32,
   vfs) as a lead-generator -> highest value. DISCOUNT/skip primitive libs (string, printf,
   font) -> FP-prone, low yield.
2. BEFORE full-kernel: scaffold drivers for the ~18 missing modules (or validate --standalone);
   build the batched all-modules sweep (per-module timeout, sequential, resumable); add a
   triage step (my judgment / human).
3. CONSIDER the design fixes: classifier-adjudicator default-ON as a downgrade guard; tighten
   realism precision on primitives (don't uphold contract-violations w/o a concrete unclamped
   attacker call site); deterministic cross-module stubbing to cut harness compile-fail churn.

---

## >>> RESUME POINT (2026-06-15, end of session) <<<
Branch reproducer-agent-merge. Everything below is recoverable from server files
(findings/JUDGMENT_NOTES.md = all adjudications + design review; git log = commits).

STATE:
- 11 commits landed (registry, token telemetry, opus-temp fix, claude-code CLI fix,
  anthropic-native complete_with_tools, 3 agentic variants, validator always-on +
  classifier->validation rename, per-component default plan, full-kernel readiness verdict).
- OPEN DECISION: make agentic realism (--enable-realism-tools / RealismToolsAgent) the
  DEFAULT under --agentic? Evidence: rt_dtb VALID -> kept read_be64 (real OOB), correctly
  demoted align4 (low-impact) via grounding audit = GOOD. rt_string was budget-contaminated
  (invalid). RE-RUNNING clean: findings/judge_rt2_string_* (string, --enable-realism-tools).

NEXT ACTION (when rt2_string DONE + not 400-contaminated):
  1. List confirmed findings (cf /tmp/rtadj.py, glob judge_rt2_string). Flat string baseline
     had 9 primitive FPs (memcpy/memset/memchr/memcmp/memset32/strncpy).
  2. If agentic realism DEMOTED those FPs (and rt_dtb reals held) -> agentic realism is
     BETTER -> edit bmc_agent/cli.py:181 so enable_realism_tools defaults True under
     --agentic (keep --no-realism-tools; preserve cost note). Validate full suite 54 +
     test_phase3.py isolated 3. Commit. Update per-component plan: realism=AGENT default.
  3. If FPs NOT demoted -> do NOT default; tools alone insufficient (need reasoning discipline).

ALSO PENDING (LLM-budget permitting):
- net/fat32 readiness modules (tools/judge_sweep.sh net fat32) — confirm parser-high-value.
- Design-fix candidates (LLM-free, 54/3-validatable): realism-call-failure should fail-safe
  to UNCERTAIN not "confirmed" (silent-fallback bug); deterministic cross-module stub defs
  to cut dynamic-harness compile-fail churn.
- API workspace budget hit its limit earlier today (regain 2026-07-01); was restored ~19:41.
  Watch for re-exhaustion; runs that 400 mid-way are INVALID (silent-fallback to confirmed).

---

## DECISION LANDED: realism = AGENT (tool-use) by DEFAULT (commit 28184d8)
Per-component plan UPDATE: realism flips from "flat" to AGENT default under --agentic.
Evidence (judgment-based, validated clean — no budget contamination):
- rt_dtb: agentic realism KEPT read_be64 (real OOB), correctly DEMOTED align4 (low-impact,
  caller-contract) via grounding audit (flagged flat verdict as narrative-only/confabulated).
- rt2_string: primitive FALSE POSITIVES 9 -> 2 (memcpy/memchr/memcmp/memset32/memmove/most-
  strncpy demoted via correct contract-vs-defect localization). Flat had confabulated them.
Mechanism: enable_realism_tools defaults True under --agentic; --no-realism-tools opts out.
Cost: more LLM round-trips per finding — partly OFFSET by adjacent-bug now default-off (4a85edf).
Caveat (recorded): agentic realism is coupled with refinement, which can add caller-unsatisfied
preconditions (the align4 over-tighten risk) — low practical harm here, but watch it.

### Updated per-component default table (the answer to "agentic vs flat per component")
- realism            -> AGENT (tool-use) default  [NEW: 28184d8]
- CEx validation     -> always-on (CBMC reachability+feasibility)  [cfbe562]
- adjacent-bug       -> OFF default (130 leads/0 bugs)  [4a85edf]
- reproducer         -> AGENT (highest-value: adds real-bug recall)
- spec_gen, bmc_config, harness_gen -> AGENT (precision / mechanical)
- refinement, feedback_distill, classifier-adjudicator (new tool variants) -> OFF until a recall win shown
- triage -> OFF

## >>> RESUME POINT (updated) <<<
Open follow-ups (LLM-budget permitting; budget hit limit earlier, regain 2026-07-01, was restored):
1. net/fat32 readiness modules (tools/judge_sweep.sh net fat32) — confirm parser-high-value.
2. LLM-FREE soundness/efficiency fixes (54/3-validatable): (a) realism-call-failure should
   fail-safe to UNCERTAIN not "confirmed" (silent-fallback bug, contaminated the first rt_string);
   (b) deterministic cross-module stub defs to cut dynamic-harness compile-fail churn.
3. Re-confirm agentic-realism precision on a 2nd FP-prone module (printf/font) when budget allows.
All decisions + adjudications in findings/JUDGMENT_NOTES.md; commits in git log.


---

## SESSION 2026-06-23 (budget-free soundness fix)

Resume followed: API budget regains 2026-07-01, so LLM sweeps would risk
400-contaminated/invalid runs -> stayed on the LLM-FREE track.

### DONE: open follow-up #2(a) — realism-gate-failure must not pass as confirmed
Commit `2022a0c6` (branch reproducer-agent-merge).
Root cause (refined): `check()` ALREADY fail-safes a failed realism LLM call to
UNCERTAIN (since 2026-05-05), but UNCERTAIN *keeps* the finding (only UNREALISTIC
demotes) AND is indistinguishable from a reasoned UNCERTAIN. So a wholesale LLM
outage (budget 400s) silently turns every finding into a kept "confirmed" one —
this is what contaminated the first rt_string sweep.
Fix (LLM-free, surgical, recall-preserving):
- `RealismCheckResult.gate_failed: bool` (+ to_dict) — set True ONLY in the
  `except LLMError` handler; intentional `_SKIPPED` stays False.
- Persisted end-to-end: bug_reporter `to_dict()` -> bug_report.json -> cli summary
  counts `gate_failed_count` and prints a LOUD "!! REALISM GATE FAILED on N ...
  UNSCREENED, NOT confirmed ... run may be contaminated" line.
- Tests: extended the skip + llm-error check() tests (gate_failed False/True);
  new `test_round_summary_flags_realism_gate_failure`.
Validated: full suite 52 failed (== baseline; pre-existing unrelated), test_phase3.py
isolated 1 failed (== baseline). NOTE: env baselines drifted from the doc's 54/3 to
52/1 on this HEAD — discipline unchanged (zero NEW failures).

### Still open (LLM-free, next autonomous candidates)
- #2(b) deterministic cross-module stub defs to cut dynamic-harness compile-fail churn.
- Budget-free track step 3: centralize output-contract validation in BaseAgent
  (prevents the non-str-leak class, fixed reactively in 3cfb465 — make it structural).
- Budget-free track step 4: pytest-randomly test ordering + faithful agent doubles.
### Still blocked on budget (regain 2026-07-01)
- net/fat32 readiness modules; re-confirm agentic-realism precision on printf/font.
