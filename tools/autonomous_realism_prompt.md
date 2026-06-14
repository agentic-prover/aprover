You are running UNATTENDED and AUTONOMOUS (the user has closed SSH). Work the saved plan
end to end without waiting for input. Default to ACTION, iterate through blockers, do not stop
early. Be honest in all status: if a gate fails or a test fails, say so.

## What to do
Execute the plan in `/home/syc/AProver/docs/realism_enforcement_plan.md`, Phases 0 -> 1 -> 2 -> 3,
IN ORDER. All work is SHADOW-ONLY. Read the plan doc each session for the authoritative phase list
(it now includes the reproducer-agent in Phase 2 and a flag-selector tools-agent in Phase 2b).

CURRENT FOCUS (start here): Phase 1 detection FIX. The harness-refiner fired 0 times on irq/vfs
because it only triggers on linker "undefined reference" errors, but the real VibeOS unit harness
NULL-DEFINES trusted globals (e.g. `fb_base = NULL`), so the link succeeds and the artifact is a
runtime NULL-deref, not a link error. FIX: detect the NULL-defined-trusted-global case (from the CBMC
counterexample showing the global = NULL and/or the harness's own NULL definitions), not just link
errors. Soundness is unchanged: calloc(1,sizeof) cannot mask a real OOB (it re-crashes -> KEPT).
Then re-run the irq/vfs shadow and verify the Phase 1 gate (wsod_* NULL-deref -> ARTIFACT/would-demote;
vfs_open_handle strcpy overflow -> REAL/kept).

After each meaningful unit of progress:
  1. Run the relevant tests / shadow validation and record the result.
  2. Commit tested, working changes to `main` (commit message trailer:
     `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`) and push to origin
     (the user's standing default is commit+push to main).
  3. Update the status file (see below).

## HARD CONSTRAINTS (never violate, even autonomously)
- Do NOT make uniform/enforcement the default. Do NOT change the `--agentic` default.
- Do NOT delete the `confirmed_dynamic` immunity special-case.
- STOP at Phase 4 — it requires explicit user sign-off. Do not begin it.
- The safety gate is absolute: cross-codebase 0/7 demoted, VibeOS 0/8 reals demoted,
  vfs_open_handle/ip_handle always kept. If any change demotes a real bug, REVERT it and stop the line.
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
