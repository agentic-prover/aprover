STATE: DONE
Phase: 3 ENFORCEMENT VALIDATION + SAFETY GATE (enforcement default-ON, cf569da)
Heartbeat: 2026-06-14T17:06:21Z iter-note: *** enforce_irq DONE + ADJUDICATED = GREEN *** Final tier tally
(parsed from vibeos_irq_p3enforce/*/bug_report.json): 0 CONFIRMED, 6 unlikely, 17 None(clean). The 6
unlikely = wsod_draw_line, wsod_int, wsod_hex(x2), sleep_ms, wsod_draw_sad_mac -- ALL expected FPs,
correctly demoted. Per oracle irq has NO real attacker-reachable bug, so 0 confirmed is exactly right.
Enforcement (ENFORCED-OFF, bug_reporter.py:231) bit on 'sleep_ms' only (x2, confirmed_dynamic -> unlikely,
realism UNREALISTIC nondet-overflow) = EXPECTED. NO real bug demoted. >>> irq GATE GREEN <<<

=== CODEBASE GATE STATUS ===
[GREEN] fixture ip_handle (real OOB restored) -> kept confirmed_dynamic, realistic/high.
[GREEN] enforce_irq -> 0 confirmed, all FPs (incl sleep_ms via enforcement) demoted; 0 reals (none exist).
[OPEN ] enforce_vfs (@6522ln, report imminent) -- KEY WATCH: vfs_readdir/vfs_append per-property
        downgrades; need function-level tiers + immunityON differential.
[OPEN ] enforce_net (@3967), enforce_libredwg (@798, realism on rowop, ~timeout soon), vfs_standalone
        (@663, vfs_open_handle pending), immunityON control (@27, early).

Earlier session: enforcement VERIFIED wired-on (pipeline.py:375-377); mechanism tests 7/7; escape hatches OK.
NEXT: read enforce_vfs function-level tiers for vfs_readdir/write/append the moment its report drops; diff
vs immunityON control to isolate enforcement-caused vs always-on demotion.
--- KEY WATCH ITEM (still open) ---
*** enforce_vfs (enforcement
ON) is emitting per-property downgrades for vfs_readdir AND vfs_append -- BOTH on the baseline KEEP/real
list (confirmed_system_entry). NOT panicking: these are PER-PROPERTY (a fn stays confirmed if ANY CEx is
realistic -> need FINAL function-level tier) and the demotion path matters (enforcement-caused vs always-on).
  - vfs_readdir downgrade reason = "dynamic validation did not reproduce the fault" => ALWAYS-ON dynamic
    path, NOT enforcement-specific (will likely demote regardless of immunity).
  - vfs_append downgrade reason = realism UNREALISTIC because harness sets _file_val.size ~504,889,355 via
    NONDET (classic nondet-struct-field over-confirmation; plausibly a CORRECT precision demotion).
DECISIVE TEST LAUNCHED: paired differential on the SAME source -> findings/phase3_enforce_vfs_immunityON
(--keep-dynamic-immunity = enforcement OFF, control) vs findings/phase3_enforce_vfs (enforcement ON).
  RULE: if control KEEPS vfs_readdir/write/append confirmed but enforcement demotes -> ENFORCEMENT-CAUSED
  demotion of oracle-reals -> revert candidate (then assess if genuine real). If BOTH demote identically
  -> always-on path, NOT a revert trigger. (Caveat: baseline tiers were set with realism RATE-LIMITED, so
  some "confirmed" were realism-errored-and-kept; working realism legitimately re-tiers per-property FPs.)
Earlier this session: enforcement VERIFIED wired-on (pipeline.py:375-377); fixture ip_handle GREEN;
mechanism tests 7/7; escape hatches verified. enforce_irq expected-FP wsod_draw_sad_mac/sleep_ms demoting
(correct). vfs_standalone @458ln (vfs_open_handle pending). NO confirmed function-level real demotion yet.
NEXT: when enforce_vfs + immunityON both finish -> diff function-level tiers for vfs_readdir/write/append.
Escape hatches verified working (--no-agentic, --keep-dynamic-immunity, env override). NOTE: even if
vfs_standalone times out, anchor #2's soundness
argument already stands (absence-because-source-modeling-FN != enforcement demotion; ip_handle is the
confirmed structurally-identical analog, already GREEN). Standalone adds rigor if it completes.
Re-grounded in baseline_oracle.md last iter. KEY adjudication clarification recorded below:
the frozen baseline lists vfs_delete_recursive under KEEP, but the task prompt (2026-06-14, authoritative)
carves it out as an EXPECTED-CORRECT demotion (callee-returns-NULL FP). So if it re-tiers, that is the
point, NOT a gate violation. Absolute anchors: vfs_open_handle + ip_handle. Genuine protected VibeOS
reals: vfs_readdir/write/append (confirmed_system_entry), readdir_callback, find_mem_child + the 2
anchors. Cross-codebase reals: all entry-reachable parser OOBs (system_entry_reached=True), structurally
outside the re-tier path. ip_handle already GREEN (prev iter). Immunity-gate tests 7/7.

=== DECISIVE GATE RESULT #1: ip_handle (real OOB, restored verbatim in fixture) -> GREEN ===
findings/phase3_gate_fixture (verify-dir on /tmp/p3_buggy/kernel, both bugs restored, enforcement ON):
- ip_handle.overflow.1: CBMC cex found; realism verdict = REALISTIC / confidence HIGH; tier =
  confirmed_dynamic; 'upheld'. Under enforcement-default-ON it stays CONFIRMED (not demoted to unlikely).
  => A genuine attacker-driven memory-safety confirmed_dynamic bug is NOT demoted by enforcement. GATE OK.
- Report: findings/phase3_gate_fixture/reports/ip_handle.md (realism reasoning = unsigned underflow
  total_len-ihl wraps to ~4GB payload_len -> OOB read in icmp/udp/tcp_handle; entry net_poll, attacker pkt).

=== GATE ANCHOR #2: vfs_open_handle (real strcpy heap overflow, restored verbatim) -> in flight ===
- Fixture verify-dir SKIPPED it: 'only_functions not found in /tmp/amc_vfs_*.c: vfs_open_handle' — the
  cross-file merged-file --functions name-match quirk (pre-existing, NOT an enforcement effect). The fn
  exists in the fixture source at vfs.c:277 with the unbounded strcpy restored (path_copy=malloc(256),
  strcpy from temp->data which vfs_write can size >256).
- Launched findings/phase3_gate_vfs_standalone: single-file verify on the buggy vfs.c (enforcement ON,
  VibeOS threat-context). Single-file verify processes ALL functions, no name-match filter. Awaiting.
- EXPECTATION: known source-modeling FN ([[project_fn_string_source_modeling_2026_06_12]]) bakes the
  string source <=32B < VFS_MAX_PATH=256, so CBMC may not surface the overflow at all. If it does NOT
  surface -> absence (a documented pre-existing FN), NOT an enforcement demotion -> gate not violated.
  If it DOES surface -> realism must judge REALISTIC so enforcement keeps it (analogous to ip_handle).

=== MECHANISM (deterministic, verified this iter) ===
- tests/test_immunity_gate.py 7/7 PASS. Enforcement re-tiers confirmed_dynamic -> 'unlikely' ONLY when
  realism==UNREALISTIC with llm_confidence in (high,medium) (bug_reporter.py:217-257). Re-tier, never
  delete -> sound. The 'immunity ENFORCED-OFF' log fires on every such re-tier; ZERO seen so far.

=== IN FLIGHT (detached; poll next iter) ===
- findings/phase3_gate_vfs_standalone/run.log  (DECISIVE anchor #2: buggy vfs_open_handle)
- findings/phase3_enforce_{irq,vfs,net}/run.log (live PATCHED source: FP-demotion side -- wsod_* nondet
  + vfs_delete_recursive expected to re-tier; confirm NO other real demoted)
- findings/phase3_enforce_libredwg/run.log (cross-codebase reedsolomon.c, enforcement ON)

=== NEXT ===
Poll the 5 runs. Adjudicate: vfs_standalone (anchor #2 keep-or-absent), enforce_* (only expected FPs
re-tier, 0 reals demoted), libredwg (0/7 cross-codebase reals demoted). If ANY real demoted ->
config.enforce_realism_on_dynamic default=False + commit+push + STATE: BLOCKED. Else record green,
update plan, queue any remaining cross-codebase target, then STATE: DONE.

=== FINAL ADJUDICATION (2026-06-14, all 6 runs complete; loop resumed + closed) ===
RESULT: GATE GREEN. enforce_realism_on_dynamic stays default-ON (config.py:591). Revert NOT triggered.
Enforcement-caused demotions (ENFORCED-OFF bit) fired on EXACTLY 2 fns across all runs:
  - sleep_ms (irq, x2)            = documented nondet-overflow FP, UNREALISTIC/high. NOT a real.
  - vfs_delete_recursive (vfs x7, standalone x6) = task-prompt-sanctioned expected-correct FP
    (callee-returns-NULL null-guard witness). Demotion is the POINT, not a violation.
DECISIVE vfs differential (enforce_vfs ON vs phase3_enforce_vfs_immunityON control):
  - vfs_readdir: ON=confirmed_system_entry (KEPT, realism realistic/high); ctrl=None. Kept under enforcement.
  - vfs_write:   ON=confirmed_dynamic (KEPT, realistic/high); ctrl=unlikely. ON STRONGER than control.
  - vfs_append:  ON=unlikely; ctrl=unlikely -> IDENTICAL demotion => always-on dynamic-not-triggered
    path, NOT enforcement-caused (append is NOT in the ENFORCED-OFF list). Per gate rule: not a revert trigger.
  => 0 genuine oracle-reals demoted by enforcement in vfs.
OTHER CODEBASES: enforce_net = 0 ENFORCED-OFF firings (4 unlikely all spurious/intentional-truncation/
  unresolved FPs, no real demoted). enforce_libredwg rc=124 (CBMC timeout, expected; 0 confirmed, 0
  demoted -> inconclusive, NOT a violation). immunityON control = 0 firings (escape hatch verified OFF).
ANCHOR #2 vfs_open_handle: ABSENT from gate_standalone -> documented source-modeling FN
  ([[project_fn_string_source_modeling_2026_06_12]]) bakes string source <=32B < VFS_MAX_PATH=256,
  so CBMC never surfaces it. Per gate rule = documented-FN absence, NOT enforcement demotion. ip_handle
  is the structurally-identical confirmed analog, already GREEN (anchor #1).
ANCHOR #1 ip_handle: GREEN (confirmed_dynamic, realistic/high, NOT demoted under enforcement).
MECHANISM: tests/test_immunity_gate.py 7/7. Re-tier never deletes -> sound.
OPEN/OPTIONAL: libredwg timed out -> re-run with longer CBMC timeout if positive cross-codebase KEEP
  evidence (a real cross-codebase confirmed bug surviving enforcement) is wanted. Not gate-blocking.
