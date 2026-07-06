# Planning Agent: unify analysis-strategy selection into the agent

## Goal
Today the analysis STRATEGY (Tier-A compositional vs Tier-B cone-slice) is chosen by
human-set env vars / CLI flags / `_is_kernel_tu` heuristics — NOT by the agent. This
is why the paper's claim ("an agent decides how to apply BMC to each program") is
currently overstated. Introduce a top-level, ADAPTIVE PlanAgent that chooses the
strategy per program and re-plans on failure, so one general bmc-agent handles both
tiers and the paper claim becomes literally true.

## Strategy knobs the planner must set (from code map)
- strategy ∈ {standalone, compositional, scoped_from_entry, cone_slice}
- entry point(s) (scoped/standalone/cone)
- property/threat: SVCOMP_PROP (memsafety|unreach-call), BMC_MEMSAFE_ONLY, arithmetic
- budget: unwind (SVCOMP_UNWIND / BMC_AGENT_CBMC_UNWIND / BMC_UNWIND_CAP), SVCOMP_TIMEOUT
- inlining/scope internals: BMC_CONE_SLICE, BMC_TRANSITIVE_INLINE, BMC_CONE_TIGHT/PROP
- target function set

## Plan schema (bmc_agent/agents/plan_agent.py)
Plan{ strategy, entry, property_class, memsafe_only, unwind, timeout, targets[],
      cone_slice, transitive_inline, rationale, fallback_ladder[] }

## PlanAgent (adaptive)
1. INITIAL plan: structural probe (call-graph size/depth, #functions, single-harness
   entry vs many, TU size, kernel-ish vs library-ish, loop structure) + LLM reasoning
   over that probe. Emit Plan.
2. TRANSLATE: Plan -> Config fields + the internal flags the env vars used to set
   (NO external env toggles). Central `apply_plan(config, plan)`.
3. RUN pipeline under the Plan.
4. RE-PLAN (bounded, K<=3) on stall: e.g. compositional whole-TU times out -> switch
   to scoped_from_entry; scoped stalls / unknown-heavy -> cone_slice; unwind too
   shallow (unwinding-assertion) -> deepen; state-explosion -> shallow + tighter cone.
   Fallback ladder is data, logged.

## Integration points
- pipeline.py: run PlanAgent before Phase 1; drive existing gated code paths from the
  Plan instead of os.environ reads (keep env as override for reproducibility).
- Replace scattered `os.environ.get("BMC_CONE_SLICE")` etc. with reads of the applied
  Plan (a single resolved config object), so behavior is agent-chosen, env-overridable.

## Phased build
P0  map knobs (DONE) + branch planning-agent (DONE)
P1  Plan dataclass + PlanAgent.initial_plan (structural probe + LLM) + apply_plan()
P2  smoke: 1 aws-c-common task (expect compositional/scoped) + 1 ldv task (expect
    cone_slice) — assert the planner picks the right strategy end-to-end
P3  adaptive re-plan ladder + bounded loop in pipeline
P4  validate: RQ1 sets (aws 27 + ldv 29) under the planner; compare to paper numbers
P5  update paper: describe the PlanAgent as the top of the orchestration; fix the 3
    wording items (floor now agent-chosen; stage order; callee-before-caller in specgen)

## Validation
Smoke first (P2), then RQ1 (P4). LLM via ANTHROPIC_API_KEY (~/.bmc_secrets). Shared
box: parallelism <=8, ulimit -v 14G.

## Progress log
- P0 DONE: knob map + branch planning-agent.
- P1 DONE: bmc_agent/agents/plan_agent.py (Plan, structural_probe, PlanAgent.initial_plan,
  apply_plan). Smoke bug caught+fixed (kernelish keyed on __VERIFIER_assert, common to both
  tiers; real signal = ldv_/driver dispatch).
- P1 DONE: cli.py `--plan` flag + hook in _cmd_verify (agent sets scope/havoc/unwind, not human).
- P2 DONE (end-to-end, Anthropic key): `verify --plan` on aws -> scope_from_entry (scoped Phase2
  to main, unwind64, CBMC verdict + realism) and on ldv -> frame_havoc (havoc=True unwind=2,
  scoped to main, agentic spec-gen). Plan drives the real pipeline; apply_plan sets the env
  knobs the modes are gated on. Uncommitted on branch planning-agent.
- MINOR: aws dynamic-validation harness had a `conflicting types` compile error (codegen, non-fatal,
  falls back) — worth a look during hardening.
- NEXT: P3 adaptive re-plan loop (climb fallback_ladder on timeout/state-explosion); P4 RQ1
  validation (aws-27 + ldv-29 under --plan) vs paper numbers; P5 paper update (planner as top of
  orchestration; the 3 wording items become agent-chosen).
