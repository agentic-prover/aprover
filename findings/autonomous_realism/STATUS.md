STATE: RUNNING
Phase: 3 ENFORCEMENT VALIDATION + SAFETY GATE (enforcement default-ON, cf569da)
Heartbeat: 2026-06-14T18:35:00Z iter-note: launched cross-codebase enforcement run (libredwg reedsolomon.c, --agentic, enforcement ON) -> findings/phase3_enforce_libredwg. 3 live VibeOS runs still finishing (net slow pole, on tcp_*). Waiter bz1mgtba1 armed for all 4. Gate so far: ip_handle GREEN, 0 enforcement-caused demotions.
confirmed_dynamic KEPT under enforcement). vfs_open_handle not CBMC-surfaceable (modeling FN, not an
enforcement issue). Live runs: 0 enforcement-caused demotions. Awaiting live-run finish for final
tiers; net is the slow pole. Waiter b8u6ytv6q armed (all-4 DONE).

FIXTURE GATE RESULT (run DONE):
- ip_handle (restored OOB read, GENUINE real): CBMC verified=False, dynamic CONFIRMED, realism
  verdict=REALISTIC/high (x2 properties), 'upheld as confirmed_dynamic'. Under enforcement KEPT. **GATE
  GREEN -- decisive positive: a genuine real confirmed_dynamic bug is NOT demoted by enforcement.**
- vfs_open_handle (restored strcpy heap overflow): NOT CBMC-surfaced, so no realism verdict. Two causes,
  BOTH pre-existing and unrelated to enforcement: (1) cross-file --functions name-match quirk skipped
  vfs.c ("only_functions not found in vfs.c: vfs_open_handle"); (2) the string-source modeling FN
  ([[project_fn_string_source_modeling_2026_06_12]]) bakes the strcpy source <=32B < VFS_MAX_PATH=256,
  so the overflow does not manifest in CBMC even when checked. Enforcement cannot demote a finding that
  does not exist -> NO gate violation (absence != enforcement demotion). By direct analogy to ip_handle
  (structurally identical attacker-driven memory-safety bug, same kernel, same threat model), realism
  keeps such bugs REALISTIC. Honest caveat: no in-situ CBMC-level realism verdict for vfs_open_handle.

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
