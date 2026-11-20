# Agent telemetry & soundness gate

Two instruments added so future `--agentic` changes (making agents flat/agentic,
routing to cheaper models, flipping defaults) can be made **measurably** and
**without silently demoting real bugs**.

## 1. Per-agent telemetry — `bmc_agent/agent_telemetry.py`

Records one entry per agent invocation: `role`, wall-clock `duration_s`,
`outcome` (ok/empty/error), and for tool agents `iterations` (LLM round-trips)
+ `tool_calls`. Thread-safe and best-effort (never raises into the pipeline).

- **Hooks:** central in `BaseAgent.run()` (covers every BaseAgent, incl. the
  tool-loop agents); manual in `JudgeAgent.judge()` (role `classifier`) and
  `AgenticHarnessGen.generate()` (role `harness_gen`).
- **Output:** `Pipeline.run()` resets at start and, at the end, logs a per-role
  table and writes `<artifact_dir>/agent_telemetry.json`
  (`{records:[...], summary:{per-role aggregates}}`).
- **Not captured yet:** tokens. `LLMClient` logs usage but doesn't return it to
  the agent layer; the schema reserves a `tokens` field (0 until plumbed).
  Follow-up: thread usage out of `complete()` / `complete_with_tools()`.

Read it: after any run, `jq .summary <artifact_dir>/agent_telemetry.json` shows
which agents fired, how often they fell through (ok vs error), and where the
wall-clock goes — the data needed to decide which agents deserve tools / a
cheaper model.

## 2. Soundness gate — two layers

### Deterministic guard — `tests/test_soundness_corpus.py` (runs in CI)
Generalizes `test_immunity_gate`. Synthetic Phase-3 reals
(`vfs_open_handle`, `ip_handle`, system-entry parser OOBs) + the static
nondet-arg FP class, fed through `BugReporter.create_report` across enforcement
ON/OFF. Asserts: reals with REALISTIC verdicts are never demoted; attack-surface
`confirmed_dynamic` keeps immunity when enforcement is off; the FP class demotes.
**Locks the tiering logic** so a future change can't silently re-tier reals. It
does NOT check the LLM's judgment — see the empirical gate.

### Empirical gate — `tools/check_soundness_gate.py` (run after a real sweep)
Codifies the manual Phase-3 adjudication. Reads the emitted `bug_report.json`
tiers (no LLM/CBMC rerun) and asserts known reals kept a `confirmed_*` tier and
known FPs demoted. Exit 1 if any real was demoted.

```
python3 tools/check_soundness_gate.py <findings_dir> \
    --reals vfs_readdir,vfs_write,vfs_open_handle \
    --fps vfs_delete_recursive,vfs_append [--strict]
# or: --manifest manifest.json   {"reals":[...], "fps":[...]}
```
An absent real is a WARNING by default (can be a documented source-modeling
false-negative, e.g. `vfs_open_handle` in Phase 3); `--strict` makes it fatal.

## Recommended next steps (the original plan, now unblocked)
1. Run a real `--agentic` sweep on a fixture; read `agent_telemetry.json` to see
   per-role cost/turns and the soundness gate to confirm GREEN. This gives the
   baseline for any change.
2. Make flat→agentic / agentic→flat changes **one role at a time** as measured
   experiments: keep ones where real-bug recall holds (soundness gate stays
   GREEN) and FP-rate/cost improve; revert the rest.
3. Cost: route high-volume/low-judgment roles to cheaper models via
   `BMC_AGENT_LLM_<ROLE>_PROVIDER` — independent of tool-use.
4. Plumb token usage into telemetry to turn "duration" into "$ per finding".
