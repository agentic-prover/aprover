You are running UNATTENDED and AUTONOMOUS (the user has closed SSH). Work the saved plan
end to end without waiting for input. Default to ACTION, iterate through blockers, do not stop
early. Be honest in all status: if a gate fails or a test fails, say so.

## What to do
Execute the plan in `/home/syc/AProver/docs/realism_enforcement_plan.md`, Phases 0 -> 1 -> 2 -> 3,
IN ORDER. All work is SHADOW-ONLY. Read the plan doc each session for the authoritative phase list
(it now includes the reproducer-agent in Phase 2 and a flag-selector tools-agent in Phase 2b).

CURRENT FOCUS (start here): Phase 3 ENFORCEMENT VALIDATION + SAFETY GATE (with auto-revert).
The user AUTHORIZED removing the confirmed_dynamic immunity (2026-06-14). It is LANDED and DEFAULT-ON:
`config.enforce_realism_on_dynamic=True` (commit cf569da) makes an UNREALISTIC realism verdict RE-TIER a
confirmed_dynamic finding to 'unlikely' (a re-tier, never a delete -> still reported -> sound). Escape
hatch: `--keep-dynamic-immunity` / `BMC_AGENT_ENFORCE_REALISM_ON_DYNAMIC=false`.

Your job now is to VALIDATE the safety gate end to end with enforcement ON, and REVERT THE DEFAULT if it
fails. Run the enforcement shadow across irq + vfs + at least one cross-codebase target (libredwg/
openjpeg/libtiff/brotli) and re-check the known VibeOS reals. GATE (absolute):
  * `vfs_open_handle` (strcpy heap overflow) and `ip_handle` (net.c OOB) stay confirmed/likely -- NOT
    demoted to 'unlikely'.
  * 0/8 VibeOS reals demoted; 0/7 cross-codebase reals demoted.
  * Expected-correct demotions: nondet-arg overflow FPs and `vfs_delete_recursive` (callee-returns-NULL)
    SHOULD re-tier to 'unlikely' -- that is the point.
If ANY real bug is demoted: set `config.enforce_realism_on_dynamic` default back to False in config.py
(revert the default; keep the flag so it stays opt-in), commit+push, set STATE: BLOCKED, and report it.
If the gate is GREEN across all codebases: record it, update the plan, set STATE: DONE.

Background (already done, do not redo): Phase 1 harness-refiner is SOUND and CORRECTLY silent on irq/vfs
(none of those findings is the boot-init-trusted NULL-global class; a CEx-witness gate was added in
5410d3a and proven against the saved counterexamples). The real FP drivers are the reachability tier
(nondet overflow) and buffer-source modeling (wsod_hex), not harness refinement.

After each meaningful unit of progress:
  1. Run the relevant tests / shadow validation and record the result.
  2. Commit tested, working changes to `main` (commit message trailer:
     `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`) and push to origin
     (the user's standing default is commit+push to main).
  3. Update the status file (see below).

## HARD CONSTRAINTS (never violate, even autonomously)
- The safety gate is ABSOLUTE: cross-codebase 0/7 reals demoted, VibeOS 0/8 reals demoted,
  vfs_open_handle/ip_handle always kept (confirmed/likely, NOT 'unlikely'). If any change demotes a
  real bug, REVERT it (for the immunity default: set config.enforce_realism_on_dynamic back to False)
  and stop the line with STATE: BLOCKED.
- The immunity removal + `--agentic` default + enforcement default are NOW USER-AUTHORIZED (2026-06-14)
  and landed. You MAY keep them on, but you must KEEP THE ESCAPE HATCHES working (--keep-dynamic-immunity,
  --no-agentic) and never make a change that DELETES a sound finding (re-tier only; soundness_policy).
- Do NOT delete a finding on an agentic judgment alone (realism/reachability/spec-soundness): re-tier only.
  Only a deterministic verifier or a self-verifying witness may delete (soundness_policy.py).
- If you hit a genuinely destructive or irreversible decision, or you are blocked and guessing would
  risk the safety gate: set STATE: BLOCKED in the status file and stop. Do not guess on irreversible actions.

## Long background runs (IMPORTANT — avoid stalling the loop)
Shadow validation runs (CBMC + realism) take 20-40 min — longer than one iteration. Launch them
DETACHED so they survive iteration boundaries: `setsid nohup <cmd> >run.log 2>&1 &`. Then on each
later iteration, POLL their logs (do not block inside one iteration on a long waiter). When a run
finishes, adjudicate it. This way each iteration does a small unit of work and returns quickly.

## Status file (REQUIRED — this is how the loop and the user track you)
Maintain `/home/syc/AProver/findings/autonomous_realism/STATUS.md`. Overwrite it EVERY iteration,
INCLUDING a fresh `Heartbeat: <UTC time> iter-note: <what you are waiting on / just did>` line —
update it even when only waiting on a background run (this is real progress and keeps the loop alive).
First line MUST be exactly one of:
  STATE: RUNNING        (still making progress, keep looping)
  STATE: BLOCKED        (stuck / needs the user — loop will stop)
  STATE: PHASE4-REACHED (Phases 0-3 done and gated green; Phase 4 needs user OK — loop will stop)
  STATE: DONE           (nothing left to do in shadow scope — loop will stop)
Then below: current phase, what you just did, test/gate results, what is next, and any decisions
the user must make. Keep it readable — it is the first thing the user sees in the morning.

## On resume
Each loop iteration re-invokes you with the same conversation. Read STATUS.md first to see where you
left off, continue from there. Also keep `docs/realism_enforcement_plan.md` and the task list updated.
