"""``PlanAgent`` — top-level analysis-strategy planner.

Decides *how* to attack a whole program with BMC, so the downstream agents
(spec inference, BMC-config, validation, refinement) work *within* that plan.
Replaces the human-set env-var strategy selection (BMC_FRAME_HAVOC, BMC_BUGHUNT,
--scope-from-entry, SVCOMP_UNWIND, ...) with an agentic, adaptive decision.

Strategy spectrum (inline as much as fits; abstract the rest):
  * scope_from_entry : single harness entry, closure small enough to inline
                       (Tier-A / aws-c-common; ~monolithic, ties CBMC).
  * frame_havoc      : single entry but closure too large to inline; inline the
                       property-reaching path, havoc the rest (Tier-B / ldv).
  * compositional    : no single harness entry (library w/ many roots); verify
                       each function against inferred contracts, stub callees.
  * standalone       : tiny whole program, inline everything directly.

Design principle (matches the rest of the system): the planner PROPOSES; the
solver disposes. A deterministic structural probe yields the decision; an
optional LLM pass may refine it; on any failure we fall back to the safe
heuristic. The plan also carries a ``fallback_ladder`` for adaptive re-planning
when a strategy stalls (timeout / state explosion / all-unknown).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from bmc_agent.logger import get_logger

if TYPE_CHECKING:
    from bmc_agent.parser import ParsedCFile

logger = get_logger("plan_agent")

# Closure-size (defined functions reachable from the entry) above which a full
# inline is presumed intractable, so we scope+havoc instead of inlining.
_INLINE_CLOSURE_MAX = 120


@dataclass
class Plan:
    strategy: str                       # scope_from_entry|frame_havoc|compositional|standalone
    entry: Optional[str] = "main"
    property_class: str = "unreach-call"  # unreach-call | memsafety
    arch: str = "LP64"                    # LP64 | ILP32 (SV-COMP data model)
    unwind: int = 64
    timeout: int = 300
    targets: Optional[set] = None        # None => all functions; {entry} => scope-from-entry
    frame_havoc: bool = False
    bughunt: bool = False
    agentic: bool = True
    rationale: str = ""
    fallback_ladder: list = field(default_factory=list)

    def summary(self) -> str:
        t = "ALL" if self.targets is None else ",".join(sorted(self.targets))
        return (f"strategy={self.strategy} entry={self.entry} prop={self.property_class} "
                f"unwind={self.unwind} havoc={self.frame_havoc} targets={t} "
                f"ladder={self.fallback_ladder}")


def structural_probe(parsed: "ParsedCFile", entry: str = "main") -> dict:
    """Deterministic structural features used to pick a strategy."""
    defined = set(getattr(parsed, "function_bodies", {}) or {}) or set(parsed.functions or {})
    cg = parsed.call_graph or {}

    # transitive closure of DEFINED functions reachable from the entry
    closure: set = set()
    stack = [entry]
    while stack:
        fn = stack.pop()
        if fn in closure:
            continue
        closure.add(fn)
        for c in cg.get(fn, ()) or ():
            if c not in closure:
                stack.append(c)
    closure_defined = closure & defined

    # roots = defined functions not called by any other defined function
    called = set()
    for f in defined:
        called |= (cg.get(f, set()) or set())
    roots = [f for f in defined if f not in called]

    names = defined
    ldv_like = sum(1 for n in names if n.startswith("ldv_"))
    # Kernel-driver signal: an LDV verification entry (ldv_main*_sequence_*),
    # module init/exit hooks, or many ldv_* shims. NOT __VERIFIER_* (both
    # SV-COMP tiers have those, so it is not a discriminator).
    driver_sig = (any(("ldv_main" in n and "sequence" in n) for n in names)
                  or "module_init" in names or "module_exit" in names)
    kernelish = (ldv_like > 3) or driver_sig

    return {
        "n_defined": len(defined),
        "closure_size": len(closure_defined),
        "has_entry": entry in defined,
        "n_roots": len(roots),
        "kernelish": kernelish,
        "ldv_like": ldv_like,
    }


class PlanAgent:
    def __init__(self, config=None, llm=None):
        self.config = config
        self.llm = llm

    def initial_plan(self, parsed: "ParsedCFile", entry: str = "main",
                     property_class: str = "unreach-call",
                     use_llm: bool = False) -> Plan:
        p = structural_probe(parsed, entry)
        logger.info("plan probe: %s", p)
        import os as _os
        _force = _os.environ.get("BMC_PLAN_FORCE_STRATEGY")
        if _force:
            _pl = self.plan_for_strategy(_force, entry=entry, property_class=property_class)
            _ft = _os.environ.get("BMC_PLAN_FORCE_TIMEOUT")
            if _ft:
                _pl.timeout = int(_ft)
            _pl.fallback_ladder = [x for x in ("frame_havoc", "scope_from_entry", "compositional") if x != _force][:1]
            logger.info("plan: FORCED strategy=%s ladder=%s", _force, _pl.fallback_ladder)
            return _pl

        if (not p["has_entry"]) or (p["closure_size"] == 0):
            plan = Plan(
                strategy="compositional", entry=None, property_class=property_class,
                unwind=(self.config.cbmc_unwind if self.config else 4), targets=None,
                frame_havoc=False, bughunt=False,
                rationale=(f"entry '{entry}' absent or empty closure "
                           f"(has_entry={p['has_entry']}, closure={p['closure_size']}, "
                           f"{p['n_roots']} roots) -> per-function compositional (stub callees). "
                           f"scope_from_entry would verify 0 fns here (vacuous)."),
                fallback_ladder=["scope_from_entry"],
            )
        elif p["kernelish"] or p["closure_size"] > _INLINE_CLOSURE_MAX:
            plan = Plan(
                strategy="frame_havoc", entry=entry, property_class=property_class,
                unwind=1, timeout=300, targets={entry}, frame_havoc=True, bughunt=True,
                rationale=(f"single entry '{entry}' but closure={p['closure_size']} defined "
                           f"fns (kernelish={p['kernelish']}) too large to inline -> "
                           f"inline property path, havoc the rest"),
                fallback_ladder=["scope_from_entry"],
            )
        else:
            plan = Plan(
                strategy="scope_from_entry", entry=entry, property_class=property_class,
                unwind=64, timeout=300, targets={entry}, frame_havoc=False, bughunt=False,
                rationale=(f"single entry '{entry}', closure={p['closure_size']} defined fns "
                           f"small enough to inline the full call closure (~monolithic)"),
                fallback_ladder=["frame_havoc"],
            )

        if use_llm and self.llm is not None:
            try:
                plan = self._llm_refine(plan, p, parsed)
            except Exception as e:  # fail-safe: keep the heuristic plan
                logger.warning("plan LLM refine failed (%s); keeping heuristic plan", e)
        return plan

    def plan_for_strategy(self, strategy: str, entry: str = "main",
                          property_class: str = "unreach-call", unwind=None) -> "Plan":
        """Build a Plan forcing a specific strategy (used by the adaptive re-plan loop)."""
        cu = (self.config.cbmc_unwind if self.config else 4)
        if strategy == "compositional":
            return Plan(strategy="compositional", entry=None, property_class=property_class,
                        unwind=cu, targets=None, frame_havoc=False, bughunt=False,
                        rationale="forced compositional (re-plan)", fallback_ladder=[])
        if strategy == "frame_havoc":
            return Plan(strategy="frame_havoc", entry=entry, property_class=property_class,
                        unwind=(unwind if unwind is not None else 1), timeout=300,
                        targets={entry}, frame_havoc=True, bughunt=True,
                        rationale=f"forced frame_havoc (re-plan; unwind={unwind if unwind is not None else 1})",
                        fallback_ladder=[])
        return Plan(strategy="scope_from_entry", entry=entry, property_class=property_class,
                    unwind=64, timeout=300, targets={entry}, frame_havoc=False, bughunt=False,
                    rationale="forced scope_from_entry (re-plan fallback)", fallback_ladder=[])

    def _llm_refine(self, plan: "Plan", probe: dict, parsed) -> "Plan":
        # Optional: let the LLM veto/adjust strategy given the probe features.
        # Kept minimal for now; heuristic is the safe default.
        return plan


def apply_plan(config, plan: "Plan"):
    """Translate a Plan into the knobs the pipeline reads. Sets the env vars the
    strategy modes are gated on (agent-chosen, not human-set) and returns the
    ``only_functions`` set for ``pipeline.run``. Env stays overridable."""
    import os
    os.environ["SVCOMP_UNWIND"] = str(plan.unwind)
    os.environ["SVCOMP_TIMEOUT"] = str(plan.timeout)
    # Property scoping: set the SHORT token the checker branches on
    # (bmc_engine/flag_selector: "unreach"|"memsafety"|"no-overflow") so
    # off-property checks (pointer/bounds/overflow) are DISABLED and only the
    # task's real property is verified. The .prp *path* form does NOT trigger
    # this override, which caused off-property pointer_dereference false alarms.
    _prop_tok = {"unreach-call": "unreach", "unreach": "unreach",
                 "memsafety": "memsafety", "no-overflow": "no-overflow"}.get(
                     plan.property_class, plan.property_class)
    os.environ["SVCOMP_PROP"] = _prop_tok
    # Data model: SV-COMP tasks are LP64; cbmc.py DEFAULTS to ILP32 (--32) when
    # SVCOMP_ARCH is unset, which makes 64-bit overflow checks wrap at 32 bits and
    # emit spurious reach_error cexs on safe tasks. Propagate the plan's data model.
    os.environ["SVCOMP_ARCH"] = getattr(plan, "arch", None) or "LP64"
    if config is not None:
        try:
            config.cbmc_unwind = plan.unwind
        except Exception:
            pass
    if plan.frame_havoc:
        os.environ["BMC_FRAME_HAVOC"] = "1"
        os.environ["BMC_BUGHUNT"] = "1"
        os.environ["BMC_TRANSITIVE_INLINE"] = "0"   # cone-inline, not transitive
        os.environ["BMC_FAITHFUL_MAIN"] = "0"       # non-faithful main (havoc off-cone)
        # LEAN: frame-havoc builds the harness mechanically from `main` (inline the
        # property-reaching cone, havoc the rest) and needs NO per-function contracts.
        # Skip the agentic phases (spec-gen tools, bmc-config agent, flag selection)
        # that otherwise run LLM calls over every function of a large driver and
        # exhaust the time budget -- the Tier-B 0/3 timeout cause. Matches the
        # specialized --no-agentic Tier-B runner (caught 18/29).
        if config is not None:
            for _f in ("enable_bmc_config_agent", "enable_flag_selection", "enable_spec_gen_tools"):
                try:
                    setattr(config, _f, False)
                except Exception:
                    pass
            try:
                config.use_legacy_spec_gen = True
            except Exception:
                pass
    else:
        for _v in ("BMC_FRAME_HAVOC", "BMC_BUGHUNT", "BMC_TRANSITIVE_INLINE", "BMC_FAITHFUL_MAIN"):
            os.environ.pop(_v, None)
    only = set(plan.targets) if plan.targets else None
    logger.info("apply_plan: %s", plan.summary())
    return only
