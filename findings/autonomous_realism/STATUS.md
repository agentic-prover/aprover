STATE: RUNNING
Phase: 4b LANDED (immunity removed, default-on) -> now Phase 3 enforcement SAFETY-GATE validation
Heartbeat: 2026-06-14T16:20:00Z iter-note: relaunched after session-limit reset (15:10 UTC). Immunity
removal authorized + landed (cf569da). Starting the enforcement safety-gate shadow sweep.

Authorized + landed this session (by the user):
- --agentic stack default-ON (74dad0b); escape hatch --no-agentic.
- Phase-1 CEx-witness gate for harness refinement (5410d3a); proven SOUND + correctly silent on irq/vfs.
- Phase-4b: confirmed_dynamic immunity REMOVED, default-ON (cf569da). enforce_realism_on_dynamic=True;
  UNREALISTIC realism now RE-TIERS dynamic findings to 'unlikely' (re-tier, never delete -> sound).
  Escape hatch: --keep-dynamic-immunity / BMC_AGENT_ENFORCE_REALISM_ON_DYNAMIC=false.

CURRENT JOB (Phase 3 enforcement validation + auto-revert):
Run the enforcement shadow (default config, enforcement ON) across irq + vfs + >=1 cross-codebase target
and re-check the VibeOS reals. ABSOLUTE GATE:
  * vfs_open_handle (strcpy heap overflow) and ip_handle (net.c OOB) stay confirmed/likely (NOT 'unlikely').
  * 0/8 VibeOS reals demoted; 0/7 cross-codebase reals demoted.
  * Expected-correct demotions: nondet-arg overflow FPs + vfs_delete_recursive (callee-returns-NULL) -> 'unlikely'.
If ANY real bug demotes: set config.enforce_realism_on_dynamic default back to False in config.py, commit+push,
STATE: BLOCKED, report. If GREEN across all codebases: record + STATE: DONE.

Constraints: re-tier only, never delete a finding on an agentic judgment (soundness_policy). Keep escape
hatches working. Commit+push tested changes to main. Co-Authored-By trailer on commits.
