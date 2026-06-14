STATE: RUNNING
Phase: 3 ENFORCEMENT VALIDATION + SAFETY GATE (enforcement default-ON, cf569da)
Heartbeat: 2026-06-14T16:25:00Z iter-note: launched 3 fresh enforcement shadow runs (vfs/irq/net)
detached with the CURRENT default config (enforce_realism_on_dynamic=True). Polling their logs next iters.

WHY FRESH RUNS: the existing phase2_retier_{irq,vfs} runs are STALE for this gate -- they ran at
14:59/15:10 local, BEFORE the immunity removal landed (cf569da @ 20:16 local), so immunity was still ON
and the real bugs were shielded by immunity rather than by a REALISTIC verdict. Worse, retier_vfs did
not even surface vfs_open_handle (LLM-transient-error nondeterminism in spec-gen). So they cannot
adjudicate the enforcement gate. Re-running fresh with enforcement default-ON.

DONE THIS ITER:
- Verified config: enforce_realism_on_dynamic default=True (config.py:580); escape hatch
  --keep-dynamic-immunity / BMC_AGENT_ENFORCE_REALISM_ON_DYNAMIC=false (cli.py:1054/1394).
- Read the enforcement code path (bug_reporter.py:217-257): enforcement re-tiers a confirmed_dynamic
  finding to 'unlikely' ONLY when realism verdict==UNREALISTIC with llm_confidence in (high,medium).
  => GATE is GREEN iff realism judges each real bug REALISTIC (or not high/med-UNREALISTIC).
- LLM smoke test PASS (native Anthropic claude-sonnet-4-6, ~1.7s).
- Unit tests PASS: tests/test_immunity_gate.py 7/7, incl. Phase-4b cases:
  * test_enforced_public_fn_unrealistic_is_retiered (vfs_open_handle + UNREALISTIC -> unlikely)
  * test_enforced_keeps_real_bug_when_realism_realistic (REALISTIC -> stays confirmed_dynamic)
  * test_enforced_is_a_retier_not_a_delete (re-tier, still reported -> sound).
- Wrote tools/validate_phase3_enforce.sh (sources ~/.config/bmc-agent/env; runs vfs/irq/net at default).

IN FLIGHT (detached, ~20-40 min each, launched 16:22 UTC):
- findings/phase3_enforce_vfs/run.log  (vfs_open_handle anchor; vfs_delete_recursive expected demote)
- findings/phase3_enforce_irq/run.log  (wsod_* nondet-arg FPs expected demote; no real to lose)
- findings/phase3_enforce_net/run.log  (ip_handle; NOTE net.c now has a total_len bounds guard at :342,
  modified Jun 12 -- ip_handle may be absent/guarded now; absence != demotion)

GATE (absolute): vfs_open_handle + ip_handle stay confirmed/likely (NOT 'unlikely'); 0/8 VibeOS reals
demoted; 0/7 cross-codebase reals demoted. Cross-codebase reals are all confirmed_system_entry (never
confirmed_dynamic), so Phase-4b structurally cannot newly-demote them (immunity it removes only ever
covered confirmed_dynamic) -- still attempting >=1 cross-codebase empirical run after VibeOS.

NEXT: poll the 3 run.logs; adjudicate realism verdicts for vfs_open_handle / ip_handle / wsod_* /
vfs_delete_recursive. If any real bug demoted -> set config.enforce_realism_on_dynamic default=False,
commit+push, STATE: BLOCKED. If green -> set up a cross-codebase enforcement run, then STATE: DONE.
