STATE: RUNNING
Phase: 3 ENFORCEMENT VALIDATION + SAFETY GATE (enforcement default-ON, cf569da)
Heartbeat: 2026-06-14T17:55:00Z iter-note: live runs show ZERO enforcement-caused demotions (0 ENFORCED-OFF lines); vfs_write stays confirmed. Fixture: ip_handle GREEN, vfs_open_handle pending. Waiters armed.
reached ip_handle: CBMC verified=False (OOB found), dynamic-validation real-bug candidate, and
**Realism verdict=REALISTIC confidence=high** => under enforcement ip_handle is KEPT (not demoted).
Awaiting vfs_open_handle (fixture now processing vfs.c). Waiters: bncil5lx8 (fixture-only, decisive)
+ b8u6ytv6q (all-4). No code change this iter.

PRELIMINARY GATE RESULT (1/2 anchors):
- ip_handle (OOB read, real): realism REALISTIC/high -> enforcement KEEPS confirmed. GATE OK.
- vfs_open_handle (heap overflow, real): pending CBMC+realism in fixture run (fixture processes files
  alphabetically; on net.c now, vfs.c near the end).

LIVE-RUN MID-STREAM (enforcement default-ON, patched source) -- ENFORCEMENT DEMOTING NOTHING SO FAR:
- ZERO 'immunity ENFORCED-OFF' lines across irq/vfs/net. Per bug_reporter.py:227-234 that log fires
  whenever enforcement would re-tier a confirmed_dynamic finding; its absence => NO confirmed_dynamic
  finding got an UNREALISTIC verdict => enforcement has demoted NOTHING. The wsod_* -> unlikely demotions
  are the reachability-tier / always-on realism path, NOT the Phase-4b immunity removal.
- vfs_write (baseline KEEP real): one property UNREALISTIC+demoted but others REALISTIC and the function
  is 'upheld as confirmed_dynamic' -> stays confirmed at function level. No real demoted.
- NOTE on baseline: the frozen baseline (2026-06-13) was created with realism LLM RATE-LIMITED (400s),
  so its tiers reflect realism-OFF behavior; now-working realism legitimately demotes some per-property
  FPs. The revert trigger is specifically enforcement-CAUSED demotion of a genuine real (tracked via
  'immunity ENFORCED-OFF' + final function tier), which is 0 so far.

*** KEY DISCOVERY THIS ITER (changes how the gate must be validated) ***
The live VibeOS tree (examples/vibeos/repo/kernel, gitignored working copy) has been PATCHED for BOTH
safety-gate anchor bugs:
- vfs_open_handle: the unbounded `strcpy(path_copy, temp->data)` is now a bounded manual loop
  (vfs.c:293-303, mtime Jun 12). Heap overflow FIXED in source.
- ip_handle: net.c:342 now has `if (total_len < ihl || total_len > len) return;`. OOB read FIXED.
Consequence: the live tree can NO LONGER prove the gate "a REAL confirmed_dynamic bug is not demoted",
because neither anchor is a live bug. Absence-because-fixed is sound (not an enforcement demotion), but
it is not a gate test. ALSO: the Jun-13 baseline realism "upheld" verdicts are UNUSABLE -- that run's
realism LLM 400'd out (workspace API limit, "regain access 2026-07-01"), so "upheld" meant
realism-errored-and-kept, not a real REALISTIC verdict. Today's native-Anthropic LLM works (smoke OK).

VALIDATION REDESIGN (the sound way to test the gate):
- Built /tmp/p3_buggy/kernel = copy of the kernel with BOTH bugs restored verbatim
  (unbounded strcpy in vfs_open_handle; total_len guard removed in ip_handle).
- Running: verify-dir --functions vfs_open_handle,ip_handle --agentic (enforcement default-ON) +
  VibeOS --threat-model-context (de-anchored realism). -> findings/phase3_gate_fixture/run.log
- GATE PASS iff realism judges both bugs REALISTIC (not high/med UNREALISTIC) so enforcement KEEPS
  them confirmed/likely. If realism wrongly demotes either -> gate FAILS -> revert default to False.
  Launcher: tools/validate_phase3_gate_fixture.sh.

IN FLIGHT (detached; waiter pid alive, re-invokes me at DONE):
- findings/phase3_gate_fixture/run.log   (DECISIVE: real bugs vfs_open_handle + ip_handle)
- findings/phase3_enforce_{vfs,irq,net}/run.log  (live patched source: FP-demotion side --
  wsod_* nondet-arg + vfs_delete_recursive expected to re-tier; confirm no OTHER real demoted)

MECHANISM FACTS (verified, hold regardless of selection nondeterminism):
- Enforcement re-tiers a finding to 'unlikely' ONLY if confidence==confirmed_dynamic AND realism
  verdict==UNREALISTIC with llm_confidence in (high,medium) (bug_reporter.py:217-257). Re-tier, not
  delete -> sound. Unit tests tests/test_immunity_gate.py 7/7 incl. Phase-4b cases.
- Cross-codebase 0/7: all those reals are confirmed_system_entry, whose downgrade path was ALREADY
  live pre-Phase-4b (baseline 0/7), so enforcement cannot newly-demote them. Empirical cross-codebase
  run queued behind VibeOS to avoid CBMC contention.

NEXT: when runs finish -> adjudicate. Fixture: vfs_open_handle/ip_handle must stay confirmed/likely.
Live: wsod_*/vfs_delete_recursive expected demote, no other real demoted. If any real demoted ->
config.enforce_realism_on_dynamic default=False + commit+push + STATE: BLOCKED. Else cross-codebase
run, then STATE: DONE.
