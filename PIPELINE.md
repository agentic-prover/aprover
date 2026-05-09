# bmc-agent Pipeline (as implemented)

ASCII reflection of `bmc_agent/pipeline.py` + `cex_validator.py` +
`realism_checker.py` + `bug_reporter.py`. Boxes marked `[L]` are
LLM-driven (agentic); `[C]` are conventional (deterministic solvers,
runtime tools, parsers). Boxes marked `(opt)` only run when the
corresponding `--enable-*` flag or env-var is set.

```
INPUTS
+----------------------+   +-----------------------------+   +-----------------+
| Source code          |   | Context (optional)          |   | Toggles         |
|                      |   |  --domain-knowledge <text>  |   |  --enable-*     |
|  --include-dir / -I  |   |  --threat-model {sec|saf|fn}|   |  --skip-refine  |
+----------+-----------+   +--------------+--------------+   +--------+--------+
           |                              |                           |
           v                              |                           |
+----------------------+                  |                           |
| Pass 1: Parse +  [C] |                  |                           |
| global call graph    |                  |                           |
+----------+-----------+                  |                           |
           |                              |                           |
           v                              |                           |
+----------------------+                  |                           |
| Pass 1.5: Domain [L] | <----------------+                           |
| Analyzer             |  (user DK appended verbatim)                 |
| headers + types      |                                              |
| + signatures         |                                              |
+----------+-----------+                                              |
           | domain knowledge                                         |
           v                                                          |
+----------------------+                                              |
| Phase 1: Spec    [L] | <---- threat-model context ------------------+
| Generator            |                                              |
| top-down + dual-spec |                                              |
+----------+-----------+                                              |
           | specs (Spec dataclass)                                   |
           v                                                          |
+----------------------+                                              |
| Phase 1.5: Flag  [L] | <---- threat-model context ------------------+
| Selector  (opt)      |  per-function picks among the backend's      |
| picks per-function   |  optional checks (overflow, conversion,      |
| backend checks       |  pointer arithmetic, ...)                    |
+----------+-----------+                                              |
           | per-function flag set                                    |
           v                                                          |
+----------------------+                                              |
| Phase 2: BMC     [C] | <---- threat-model baseline -----------------+
| Engine               |  (security: pointer + bounds checks          |
| harness gen +        |   safety:   adds div-by-zero check           |
| BMC backend          |   functional: baseline off)                  |
| k=4 unwind, 120s     |                                              |
+----------+-----------+
           | counterexamples
           v
+-----------------------+
| CEx Dedup        [C]  |  one per (function, property_type);
| _dedup_counterexamples|  assertion.N kept in full
+----------+------------+
           |
           v
+--------------------------------------------------------------+
| Phase 3: CEx Validator                                       |
|  +-------------------------------------------------------+   |
|  | Stage 1: Input reachability         [L + C]           |   |
|  |   can a caller produce the CEx state? (BMC sub-query) |   |
|  +-------------------------------------------------------+   |
|  | Stage 2: Callee feasibility         [C]               |   |
|  |   re-run BMC with real callees vs. stubs              |   |
|  +-------------------------------------------------------+   |
|  | Stage 3: Dynamic harness  (opt)     [C]               |   |
|  |   compile + run a reproducer; signal handlers catch   |   |
|  |   crashes from source-level checks                    |   |
|  +-------------------------------------------------------+   |
+----------+-------------------+--------------------+----------+
           | real_bug          | spurious           | unresolved
           v                   v                    v
+--------------------+  +-----------------+  +------------------+
| Realism Audit  [L] |  | Refiner    [L]  |  | Track + skip     |
|   (opt)            |  | propose tighter |  | (no spec change) |
| classifies         |  | precondition    |  +------------------+
| {realistic|        |  +--------+--------+
|  unrealistic|      |           |
|  uncertain}        |           v
| may downgrade tier |  +-----------------+
+---------+----------+  | Soundness  [C]  |
          |             | Guard           |
          v             | BMC over-refine |
+-------------------+   | check (LLM      |
| BugReport         |   | fallback)       |
| + confidence tier |   +--------+--------+
| + reasoning trail |    accepted | rejected
+-------------------+            v   |
                                 |   v
                                 |  UNRESOLVED
                                 |
                  +--------------+
                  | refined spec
                  v
        +---------------------+
        | Phase 3c: re-run [C]|
        | BMC on refined fn   |  <-- CEGAR self-recheck
        +---------+-----------+
                  |
                  v
        +---------------------+
        | Phase 3b: re-run [C]|
        | BMC on its callers  |  <-- compositional propagation
        +---------+-----------+
                  |
                  +--> back into Phase 3 validation (capped re-queue)

(After Phase 3 settles)
+----------------------------+
| Phase 4: Spec Quality (opt)|  [L + C]
|  mutation testing,         |  BMC_AGENT_ENABLE_SPEC_QUALITY=true
|  coverage, consistency,    |
|  executable sanity         |
+----------------------------+

OUTPUTS (written to artifacts/<driver>/<file>/<function>/)
  spec.json          refinement_history.json
  cbmc_result.json   bug_report.json (per confirmed bug)
  classification.json (per CEx)
  harness.<ext>      propagation_events.json
```

## Tier assignment (`bug_reporter.create_report`)

```
+--------------------------+--------------------------------------------+
| confirmed_dynamic        | Stage 3 reproducer crashed on a            |
|                          | *source-level* check                       |
+--------------------------+--------------------------------------------+
| confirmed_system_entry   | Stage 1 traced the CEx state to a          |
|                          | no-caller (system-entry) function          |
+--------------------------+--------------------------------------------+
| confirmed_bmc            | At least one direct caller can reach the   |
|                          | CEx state, full chain not yet traced       |
+--------------------------+--------------------------------------------+
| likely                   | Soundness guard rejected a tightening      |
|                          | (assumed real to avoid suppression)        |
+--------------------------+--------------------------------------------+
| unlikely                 | Realism Audit returned `unrealistic`       |
|                          | with high/medium confidence (and the bug   |
|                          | was NOT confirmed_dynamic from a           |
|                          | source-level property)                     |
+--------------------------+--------------------------------------------+
```

`confirmed_dynamic` is *immune* to realism downgrade except when the
failing property is `main.assertion.N` — those assertions encode the
LLM-generated postcondition (harness noise), not source semantics.

## Component status

| Component                 | File                                | Status       |
|---------------------------|-------------------------------------|--------------|
| Pass 1 parse              | `parser.py`, `preprocessor.py`      | always on    |
| Pass 1.5 Domain Analyzer  | `domain_analyzer.py`                | always on    |
| Phase 1 Spec Generator    | `spec_generator.py`                 | always on    |
| Phase 1.5 Flag Selector   | `flag_selector.py`                  | `--enable-flag-selection` |
| Phase 2 BMC Engine        | `bmc_engine.py`                     | always on    |
| CEx Dedup                 | `pipeline.py:_dedup_counterexamples`| always on    |
| Stage 1+2 validation      | `cex_validator.py`                  | always on    |
| Stage 3 Dynamic harness   | `dynamic_validator.py`              | `--enable-dynamic-validation` |
| Realism Audit             | `realism_checker.py`                | `--enable-realism-check` |
| Refiner + Soundness Guard | `cex_validator.py`                  | always on (`--skip-refinement` disables) |
| Phase 3c / 3b CEGAR       | `pipeline.py`                       | always on    |
| Phase 4 Spec Quality      | `spec_quality.py`                   | `BMC_AGENT_ENABLE_SPEC_QUALITY=true` |
| Bug Reporter              | `bug_reporter.py`                   | always on    |
| BMC backend abstraction   | `backends/bmc_backend.py`           | pluggable    |
