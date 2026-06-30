# Experiment Plan: "full claude-code agents" vs default bmc-agent (ablation)

Status: PLANNED — start after the CCC curated sweep (171 files) completes.
Owner: the team. Server: `~/AProver`, branch `reproducer-agent-merge`.

## 1. Research question / hypothesis
Does wrapping each pipeline stage in a **tool-using, code-reading Claude Code agent**
(reads the actual source tree, multi-turn, self-correct) improve bug-finding **accuracy,
coverage, or precision** over the default flat-completion pipeline — and at what cost?

Primary hypothesis: the win is in **precision (fewer false positives, realism grounded
in real code)** and **harness-generation coverage**, NOT necessarily raw bug count.

## 2. The two arms (HOLD MODEL CONSTANT = the key confound control)
Both arms: `--agentic` pipeline ON, model **claude-sonnet-4-6**, same files, same
`--per-function-time-budget`, same realism-as-oracle eval. Only the agent-ness differs.

- **DEFAULT arm** (baseline = what the sweeps ran):
  provider `anthropic` (metered key), flat API completions,
  `claude_code_agentic=False`, `enable_agentic_harness=False`.
- **FULL-AGENT arm** (treatment):
  provider `claude-code` + `claude_code_agentic=True`
  (+ likely `enable_agentic_harness=True`, `--specs-via-claude-code`).
  CONFIRM the exact flag set with the team — these are SEPARABLE ablations:
    (a) `claude_code_agentic` alone (agentic realism/spec reasoning, reads code)
    (b) + `enable_agentic_harness` (code-reading harness builder)
    (c) + `--specs-via-claude-code` (agentic spec generation)
  Consider running (a), (a+b), (a+b+c) as a small ablation ladder if budget allows.

CONFOUND NOTE: claude-code CLI on the **team account** uses
sonnet-4-6 (verified) — same model as the anthropic arm. Do NOT let the full arm drift to
opus/CLI-default, or you measure model+harness together. claude-code arm has a subscription
spending cap → spread over reset windows or it stalls; this alone forbids whole-codebase.

## 3. Why a SUBSET (not whole vibeos/ccc) — this is the correct design, not a compromise
1. Cost: full agents are multi-turn + tool-using; claude-code cap makes whole-tree infeasible.
2. 0-vs-0 files give no comparative signal — concentrate on files that DISCRIMINATE.
3. Nondeterminism (run-to-run tier flips observed) → need N>=3 repeats/file/arm → forces subset.
4. Rigor: ablation-on-representative-subset-with-repeats is the standard expectation.

## 4. Subset selection (SAME files both arms; N=3 repeats each)
Re-run BOTH arms FRESH under identical conditions — do NOT compare the full arm against the
existing sweep numbers (those were mixed-condition: vibeos partly capped-claude-code, ccc
anthropic @180s). Apples-to-apples requires both arms re-run now.

### Positive set (measures RECALL of known real bugs + NEW bugs)
- vibeos (C kernel, CBMC): `elf` (~5 distinct: NULL-deref, calc_size overflow, load_at
  p_vaddr write, relocations arbitrary-write, phdr OOB), `vfs` (append overflow, readdir
  name_size-1 underflow OOB, write overflow), `memory` (calloc overflow), `fb` (y+16
  unsigned-overflow OOB), `dtb` (dtb_parse / read_be64 pointer arith).
- ccc (Rust, kani): `common/long_double` (7: f128->int off-by-one overflow, f128_rem
  div-by-zero, x87 soft-float asserts), `ir/analysis` (intersect dataflow assertion).
  >>> FINALIZE after the 171-file sweep finishes — add any frontend/elf/linker/x86 findings.

### Hard-negative controls (measures precision: FPs introduced vs TPs default missed)
- ~3-4 files per project that the default marked CLEAN, ideally with subtle byte/offset logic
  (e.g. ccc `ir/constants`, a curated-backend `linker_common/parse_object` if it came clean;
  vibeos a clean parser module). Watch whether the full arm adds FPs or surfaces a missed TP.

### Coverage probes (measures harness-gen coverage win — independent of bug count)
- ccc files with `&self`-method / crate-type-param functions the DETERMINISTIC builder could
  not harness (A2 gap: crate-type param construction). Test whether `enable_agentic_harness`
  lifts functions-harnessed. Candidates: files using structs like `Lexer`, recursive `Expr`.

Target size: ~12-20 files per project.

## 5. Metrics (the comparison table, per arm, averaged over repeats + variance)
- realism-confirmed findings (count)
- RECALL of the known-bug positive set (did it reproduce each known bug?)
- NEW bugs (not in default's set) — adjudicate with realism + human judgment as oracle
- FALSE-POSITIVE rate (precision)
- harness-generation COVERAGE: functions harnessed / total harnessable
- COST: output tokens + $ + wall-clock per file and per confirmed finding
- DETERMINISM: variance of findings/verdicts across the 3 repeats

## 6. Execution mechanics (build these in the "later" session)
- Two resilient runner scripts mirroring `tools/ccc_full_sweep.sh` /
  `tools/kernel_sweep_anthropic.sh`: `tools/ablation_default.sh`, `tools/ablation_fullagent.sh`.
  Same FILE LIST (a shared `tools/ablation_files.txt`), same budget, N=3 (loop, distinct output
  dirs per repeat). Detached (`nohup setsid`), resumable via done-markers, watchdog cron.
- KEEP THE ORPHAN-REAP FIX: process-group killpg on timeout is in kani.py (commit baabe29);
  for the claude-code arm, also watch for orphaned `claude` CLI procs — add exact-comm cleanup.
- claude-code arm: export nothing for ANTHROPIC_API_KEY (so resolved_provider -> claude-code),
  pass `--model claude-sonnet-4-6` explicitly (CLI default opus-4-1 404s on team acct), set
  `--claude-code-agentic` (+ harness/specs flags per confirmed arm). Respect the spending cap:
  serial, backoff, spread across reset windows.
- Aggregate to `findings/ablation/SUMMARY.tsv` (arm, project, file, repeat, findings, recall,
  FPs, harnessed, tokens, wall-clock) -> the paper table.

## 7. Open questions to resolve with the team BEFORE running
1. Exact FULL-arm flag set — (a) / (a+b) / (a+b+c) ladder, or just one config?
2. N repeats — 3 (default) or more for tighter variance bars?
3. Which account for the claude-code arm (team metered vs Max) given the cap?
4. Final subset size and the exact control/probe files.

## 8. Paper framing
"Ablation: does per-stage code-reading agency improve precision/coverage over flat-prompt
bmc-agent, at what token/$ cost?" Report on a curated representative subset with repeats —
standard for an ablation; do not claim whole-codebase.
