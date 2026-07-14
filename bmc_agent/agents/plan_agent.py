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

import re as _re_cost

# --- Tractability cost model (family-agnostic) ---------------------------
# Strategy follows from an ESTIMATE of the formula CBMC would build if it
# inlined the whole call closure from an entry, compared against ONE global
# budget. No benchmark family is named or pattern-matched: a kernel driver
# lands in frame_havoc because its closure is genuinely expensive (large +
# data-dependent nested loops), not because it "looks like LDV"; a small task
# lands in scope_from_entry because it is cheap. This replaces both the fixed
# closure-size threshold and the LDV/SV-COMP structural shortcuts.
#
# Units are rough "gates" (unrolled instruction count). The budget is a single
# calibration point -- fit once by regressing this estimate against observed
# CBMC solve cost on a HETEROGENEOUS corpus; it is not a per-family constant.
# Calibrated 2026-07-06 against 10 real vibeos closures run through CBMC
# (--bounds-check --pointer-check, unwind 8): ALL solved in <=32s at estimates up
# to ~693k, so 300k needlessly demoted tractable closures to frame_havoc. Raised to
# 3M (inline what CBMC handles) with the informed fallback (record_scope_blowup /
# _effective_budget) as the real safety net -- the a-priori estimate is a weak
# predictor (it over-counted a wide-but-shallow closure ~250x), so the budget
# self-lowers below the smallest OBSERVED scope blowup rather than trusting the guess.
_INLINE_COST_BUDGET = 3_000_000
_W_MEM = 2          # array index / pointer deref -> array-theory constraints
_W_NL = 4           # nonlinear / bitwise op on non-constants -> SAT-hard
_UNWIND_DEEP_DEFAULT = 64    # solve-time unwind when loop bounds aren't all constant
_UNWIND_CAP = 256           # never derive an unwind above this

_LOOP_KW = _re_cost.compile(r"\b(for|while|do)\b")
_MEM_OP = _re_cost.compile(r"\[|->")
_NL_OP = _re_cost.compile(r"<<|>>|[*/%]")
_CONST_CMP = _re_cost.compile(r"[<>]=?\s*(\d+)\b")


def _strip_comments_min(s: str) -> str:
    s = _re_cost.sub(r"/\*.*?\*/", " ", s or "", flags=_re_cost.DOTALL)
    return _re_cost.sub(r"//[^\n]*", " ", s)


def _max_loop_depth(body: str) -> int:
    """Max loop-nesting depth in a function body. Char scanner that SKIPS
    parenthesised regions (so ``for(;;)`` header semicolons don't interfere)
    and tracks which open brace-blocks are loop bodies. Family-agnostic;
    braced loops are exact. Known approximation: brace-less loop bodies
    (``for(..) stmt;``) are not counted -- they are single statements, and the
    undercount biases toward inlining, which the fallback ladder recovers from.
    """
    stack = []          # 'L' (loop body) or 'O' (other block)
    pending = False     # loop keyword seen, awaiting its '{' (header skipped)
    maxd = 0
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "(":                       # skip parenthesised region (headers, call args)
            depth, i = 1, i + 1
            while i < n and depth:
                if body[i] == "(":
                    depth += 1
                elif body[i] == ")":
                    depth -= 1
                i += 1
            continue
        if ch == "{":
            stack.append("L" if pending else "O")
            pending = False
            d = stack.count("L")
            if d > maxd:
                maxd = d
            i += 1
            continue
        if ch == "}":
            if stack:
                stack.pop()
            i += 1
            continue
        if ch == ";":
            pending = False
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (body[j].isalnum() or body[j] == "_"):
                j += 1
            if body[i:j] in ("for", "while", "do"):
                pending = True
            i = j
            continue
        i += 1
    return maxd


def _function_cost(body: str, est_unwind: int) -> float:
    """Estimated unrolled formula size for one function body."""
    b = _strip_comments_min(body)
    base = b.count(";") + _W_MEM * len(_MEM_OP.findall(b)) + _W_NL * len(_NL_OP.findall(b))
    depth = _max_loop_depth(b)
    return base * (est_unwind ** depth if depth > 0 else 1)


def _closure_defined(cg, defined, entry):
    seen = set()
    stack = [entry]
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        for g in (cg.get(f, ()) or ()):
            if g not in seen:
                stack.append(g)
    return seen, (seen & defined)


def _has_cycle(cg, nodes) -> bool:
    nodes = set(nodes)
    color = {}
    def dfs(u):
        color[u] = 1
        for v in (cg.get(u, ()) or ()):
            if v not in nodes:
                continue
            c = color.get(v, 0)
            if c == 1 or (c == 0 and dfs(v)):
                return True
        color[u] = 2
        return False
    for n in list(nodes):
        if color.get(n, 0) == 0 and dfs(n):
            return True
    return False


def _derive_unwind(parsed: "ParsedCFile", entry: str) -> int:
    """Solve-time unwind from the closure's loop bounds: fully unroll when every
    loop has a compile-time constant bound (sound); else a deep default."""
    bodies = getattr(parsed, "function_bodies", {}) or {}
    cg = parsed.call_graph or {}
    defined = set(bodies) or set(getattr(parsed, "functions", {}) or {})
    _, clos = _closure_defined(cg, defined, entry)
    max_const, all_const, any_loop = 0, True, False
    for fn in clos:
        b = _strip_comments_min(bodies.get(fn, ""))
        for lm in _LOOP_KW.finditer(b):
            any_loop = True
            hdr = b[lm.end(): lm.end() + 80]
            cm = _CONST_CMP.search(hdr)
            if cm:
                max_const = max(max_const, int(cm.group(1)))
            else:
                all_const = False
    if any_loop and all_const:
        return min(max_const + 1, _UNWIND_CAP)
    return _UNWIND_DEEP_DEFAULT


def estimate_inline_cost(parsed: "ParsedCFile", entry: str, est_unwind: int) -> float:
    """Estimate CBMC formula size for inlining the whole closure from `entry`."""
    bodies = getattr(parsed, "function_bodies", {}) or {}
    cg = parsed.call_graph or {}
    defined = set(bodies) or set(getattr(parsed, "functions", {}) or {})
    seen, clos = _closure_defined(cg, defined, entry)
    total = sum(_function_cost(bodies.get(fn, ""), est_unwind) for fn in clos)
    if _has_cycle(cg, clos):     # recursion -> bounded re-entry inflates cost
        total *= est_unwind
    return total


# --- Informed fallback: self-calibrate the budget from OBSERVED scope blowups ----
# When scope_from_entry is chosen (est <= budget) but the inline verification then
# exhausts resources (timeout/OOM), that is a labelled mispredict: the estimate was
# too low. We append the label and, on future plans, drop the effective budget just
# below the smallest observed blowup so similar closures go straight to frame_havoc.
# This closes the loop the manual CBMC calibration opened -- the planner learns the
# real intractability boundary from runs instead of trusting the a-priori guess.
import os as _os_cal
import json as _json_cal

def _calib_path() -> str:
    return _os_cal.environ.get(
        "BMC_COST_CALIB_LOG", _os_cal.path.expanduser("~/.bmc_cost_calibration.jsonl"))

def record_scope_blowup(entry: str, estimate: float, unwind: int) -> None:
    """Record that scope_from_entry(entry) exhausted resources at this estimate.

    Ignores implausibly-small estimates: a genuine COST blowup only happens on a
    large closure. A "blowup" at a tiny estimate is a mis-recorded non-cost timeout
    (LLM hang / harness error) and must NOT poison the budget (the est=54 incident
    that collapsed the budget to 49 and forced everything to frame_havoc)."""
    if float(estimate) < 0.1 * _INLINE_COST_BUDGET:
        logger.debug("record_scope_blowup: ignoring implausibly-small blowup est=%.0f for %r",
                     float(estimate), entry)
        return
    try:
        with open(_calib_path(), "a") as _fh:
            _fh.write(_json_cal.dumps({"entry": entry, "estimate": float(estimate),
                                       "unwind": int(unwind), "outcome": "scope_blowup"}) + "\n")
    except Exception as _e:
        logger.debug("record_scope_blowup skipped (%s)", _e)

def _effective_inline_budget() -> float:
    """Budget lowered to just under the smallest OBSERVED scope blowup (or the static
    budget if none). A single real resource-exhaustion is trusted over the estimate."""
    b = float(_INLINE_COST_BUDGET)
    try:
        p = _calib_path()
        if _os_cal.path.exists(p):
            _floor = 0.1 * _INLINE_COST_BUDGET   # never collapse below 10% of static
            blown = []
            for _ln in open(p):
                try:
                    _r = _json_cal.loads(_ln)
                    # trust only plausibly-large blowups; a tiny-estimate "blowup" is
                    # a mis-recorded non-cost timeout (the est=54 poison)
                    if _r.get("outcome") == "scope_blowup" and float(_r["estimate"]) >= _floor:
                        blown.append(float(_r["estimate"]))
                except Exception:
                    continue
            if blown:
                b = max(_floor, min(b, min(blown) * 0.9))
    except Exception as _e:
        logger.debug("_effective_inline_budget fell back to static (%s)", _e)
    return b


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
    func_props: Optional[dict] = None   # per-function property class (code-shape inference)
    est_cost: float = 0.0               # cost-model estimate for the chosen scope (0 = N/A)

    def summary(self) -> str:
        t = "ALL" if self.targets is None else ",".join(sorted(self.targets))
        return (f"strategy={self.strategy} entry={self.entry} prop={self.property_class} "
                f"unwind={self.unwind} havoc={self.frame_havoc} targets={t} "
                f"ladder={self.fallback_ladder}")


# Code shapes that make integer-overflow checking worthwhile (size/pointer arithmetic that
# can overflow -> undersized alloc / bad index). Functions matching this get property class
# "all" (memsafety + overflow); everything else stays "memsafety" (bounds+pointer only, low FP).
import re as _re_arith
_ARITH_SHAPE = _re_arith.compile(
    r"<<|>>"
    r"|\b(?:k?z?alloc|k?malloc\w*|kmalloc_array\w*|calloc|realloc\w*|vmalloc\w*|reallocarray)\s*\([^;{}]*\*"
    r"|\b(?:n|len|size|count|num|nmemb|width|height|stride|cap|capacity|nbytes|bytes|sz)\b\s*\*\s*[\w(]"
    r"|[\w)]\s*\*\s*\b(?:n|len|size|count|num|nmemb|width|height|stride|cap|capacity|nbytes|bytes|sz)\b"
)

def infer_function_properties(parsed: "ParsedCFile", base_class: str = "memsafety") -> dict:
    """Per-function property-class inference (PlanAgent, real code only). memsafety everywhere;
    add overflow (-> class "all") on functions doing size/pointer arithmetic, where integer
    overflow can feed a memory-safety bug. Spec/postcondition asserts ride along regardless."""
    bodies = getattr(parsed, "function_bodies", {}) or {}
    names = list(bodies.keys()) or list(getattr(parsed, "functions", {}) or {})
    m = {}
    for fn in names:
        body = bodies.get(fn, "") or ""
        m[fn] = "all" if _ARITH_SHAPE.search(body) else base_class
    return m


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

    return {
        "n_defined": len(defined),
        "closure_size": len(closure_defined),
        "has_entry": entry in defined,
        "n_roots": len(roots),
    }


class PlanAgent:
    def __init__(self, config=None, llm=None):
        self.config = config
        self.llm = llm

    def initial_plan(self, parsed: "ParsedCFile", entry: str = "main",
                     property_class: str = "memsafety",
                     use_llm: bool = False) -> Plan:
        p = structural_probe(parsed, entry)
        logger.info("plan probe: %s", p)
        # Re-anchor: if the requested entry (e.g. 'main') is absent but the file has a
        # defined "caller-root" (a root function that calls another defined function),
        # verify that root with its callees INLINED. That exercises call-site checks
        # (caller-misuse: a bad buffer/len passed into a callee) which per-function
        # isolation with stubbed callees silently misses. Pick the smallest such root.
        import os as _os_ra
        if not p["has_entry"] and not _os_ra.environ.get("BMC_ABLATE_REANCHOR"):
            _cg = parsed.call_graph or {}
            _defd = set(getattr(parsed, "function_bodies", {}) or {}) or set(parsed.functions or {})
            _called = set()
            for _f in _defd:
                _called |= (_cg.get(_f, set()) or set())
            _caller_roots = [_f for _f in _defd
                             if _f not in _called and (_defd & (_cg.get(_f, set()) or set()))]
            # Only re-anchor+scope onto a HARNESS-SHAPED root: a NO-ARGUMENT driver
            # (main/bad-style) that sets up inputs and calls a callee -- the caller-
            # misuse case (b3). Scoping onto a *parameterised* library root would
            # verify ONLY that one function and skip every other one (the coverage-
            # collapse bug: 1/9, 1/54, 1/13). Such files stay COMPOSITIONAL so ALL
            # functions are verified.
            def _no_args(_r):
                try:
                    _ps = (parsed.get_function_info(_r).signature.parameters) or []
                    _ps = [1 for (_t, _n) in _ps
                           if (_n or (str(_t).strip() not in ("void", "")))]
                    return len(_ps) == 0
                except Exception:
                    return False
            _harness_roots = [_r for _r in _caller_roots if _no_args(_r)]
            _scoped = False
            if len(_harness_roots) == 1:
                _new = _harness_roots[0]
                _p2 = structural_probe(parsed, _new)
                # Scope ONLY if this root's closure covers ~ALL defined functions
                # (a true micro-harness, e.g. b3 {bad,sum}). A no-arg *library* fn
                # (e.g. memory_init) whose closure is a small fraction must NOT scope
                # -- that re-collapses coverage; such files stay compositional.
                if _p2["closure_size"] >= max(1, p["n_defined"] - 1):
                    logger.info("plan: requested entry %r absent; re-anchored on no-arg caller-root "
                                "%r (micro-harness, closure=%d/%d) to exercise call-site checks",
                                entry, _new, _p2["closure_size"], p["n_defined"])
                    entry, p = _new, _p2
                    _scoped = True
            if not _scoped:
                logger.info("plan: requested entry %r absent (%d caller-root(s), %d harness-shaped, "
                            "none cover the file) -> compositional (verify ALL functions)",
                            entry, len(_caller_roots), len(_harness_roots))
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
        else:
            _uw = _derive_unwind(parsed, entry)
            _cost = estimate_inline_cost(parsed, entry, _uw)
            _budget = _effective_inline_budget()
            if _budget < _INLINE_COST_BUDGET:
                logger.info("plan cost model: effective budget lowered to %.0f (from %d) "
                            "by observed scope blowups", _budget, _INLINE_COST_BUDGET)
            logger.info("plan cost model: entry=%r closure=%d est_unwind=%d "
                        "inline_cost=%.0f budget=%.0f", entry, p["closure_size"],
                        _uw, _cost, _budget)
            if _cost > _budget:
                plan = Plan(
                    strategy="frame_havoc", entry=entry, property_class=property_class,
                    unwind=1, timeout=300, targets={entry}, frame_havoc=True, bughunt=True,
                    rationale=(f"single entry '{entry}': estimated inline cost {_cost:.0f} > "
                               f"budget {_budget:.0f} (closure={p['closure_size']} fns, "
                               f"unwind~{_uw}) -> too costly to inline; inline property path, "
                               f"havoc the rest"),
                    fallback_ladder=["scope_from_entry"], est_cost=_cost,
                )
            else:
                plan = Plan(
                    strategy="scope_from_entry", entry=entry, property_class=property_class,
                    unwind=_uw, timeout=300, targets={entry}, frame_havoc=False, bughunt=False,
                    rationale=(f"single entry '{entry}': estimated inline cost {_cost:.0f} <= "
                               f"budget {_budget:.0f} (closure={p['closure_size']} fns, "
                               f"unwind~{_uw}) -> inline the full call closure"),
                    fallback_ladder=["frame_havoc"], est_cost=_cost,
                )

        if plan.property_class in ("memsafety", "all"):
            try:
                plan.func_props = infer_function_properties(parsed, base_class="memsafety")
                _n_ovf = sum(1 for v in plan.func_props.values() if v == "all")
                logger.info("[PlanAgent] per-function property inference: %d/%d functions get "
                            "overflow (size/ptr arithmetic); rest memsafety-only",
                            _n_ovf, len(plan.func_props))
            except Exception as _e:
                logger.warning("[PlanAgent] func-property inference failed (%s); using global %s",
                               _e, plan.property_class)
        if use_llm and self.llm is not None:
            try:
                plan = self._llm_refine(plan, p, parsed)
            except Exception as e:  # fail-safe: keep the heuristic plan
                logger.warning("plan LLM refine failed (%s); keeping heuristic plan", e)
        return plan

    def plan_for_strategy(self, strategy: str, entry: str = "main",
                          property_class: str = "unreach-call", unwind=None,
                          template: Optional["Plan"] = None) -> "Plan":
        """Build a Plan forcing a specific strategy (used by the adaptive re-plan loop)."""
        if template is not None:
            property_class = getattr(template, "property_class", property_class)
        cu = (self.config.cbmc_unwind if self.config else 4)
        if strategy == "compositional":
            plan = Plan(strategy="compositional", entry=None, property_class=property_class,
                        unwind=cu, targets=None, frame_havoc=False, bughunt=False,
                        rationale="forced compositional (re-plan)", fallback_ladder=[])
            return self._inherit_plan_context(plan, template)
        if strategy == "frame_havoc":
            plan = Plan(strategy="frame_havoc", entry=entry, property_class=property_class,
                        unwind=(unwind if unwind is not None else 1), timeout=300,
                        targets={entry}, frame_havoc=True, bughunt=True,
                        rationale=f"forced frame_havoc (re-plan; unwind={unwind if unwind is not None else 1})",
                        fallback_ladder=[])
            return self._inherit_plan_context(plan, template)
        plan = Plan(strategy="scope_from_entry", entry=entry, property_class=property_class,
                    unwind=64, timeout=300, targets={entry}, frame_havoc=False, bughunt=False,
                    rationale="forced scope_from_entry (re-plan fallback)", fallback_ladder=[])
        return self._inherit_plan_context(plan, template)

    @staticmethod
    def _inherit_plan_context(plan: "Plan", template: Optional["Plan"]) -> "Plan":
        """Carry non-strategy planner decisions across adaptive retries."""
        if template is None:
            return plan
        plan.arch = getattr(template, "arch", plan.arch)
        plan.timeout = getattr(template, "timeout", plan.timeout)
        plan.agentic = getattr(template, "agentic", plan.agentic)
        if getattr(template, "func_props", None) is not None:
            plan.func_props = dict(template.func_props)
        return plan

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
                 "memsafety": "memsafety", "no-overflow": "no-overflow",
                 "all": "all"}.get(
                     plan.property_class, plan.property_class)
    os.environ["SVCOMP_PROP"] = _prop_tok
    import json as _json_fp
    if getattr(plan, "func_props", None):
        os.environ["BMC_FUNC_PROP_MAP"] = _json_fp.dumps(plan.func_props)
    else:
        os.environ.pop("BMC_FUNC_PROP_MAP", None)
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
        # SV-COMP reachability tasks are judged by the solver, not by LLM-written
        # contracts. In scope_from_entry the planner already picked the concrete
        # entry and unwind; spending the Codex/Claude budget drafting caller-
        # grounded specs for the entry closure can exhaust the wall clock before
        # CBMC runs at all. Use deterministic permissive specs here so Codex mode
        # reaches Phase 2. Keep this PlanAgent-owned: runners still pass only
        # --plan --svcomp and never choose suite-specific strategy knobs.
        if (
            os.environ.get("BMC_SVCOMP_MODE")
            and plan.strategy == "scope_from_entry"
            and _prop_tok == "unreach"
            and config is not None
        ):
            for _f in ("enable_bmc_config_agent", "enable_flag_selection", "enable_spec_gen_tools"):
                try:
                    setattr(config, _f, False)
                except Exception:
                    pass
            try:
                config.use_legacy_spec_gen = True
                config.lite_mode = True
                config.lite_with_contracts = False
            except Exception:
                pass
            logger.info(
                "apply_plan: SV-COMP unreach scope_from_entry lean mode "
                "(permissive deterministic specs; no BMC-config/spec-gen LLM)"
            )
    # Caller-side precondition checking (compositional caller-misuse detection).
    # In compositional mode callees are STUBBED, so by default a caller passing an
    # out-of-contract buffer/size to the stub is ASSUMED away (the soundness hole).
    # Turn on BOUNDS-ONLY assert mode: the memory-safety clauses of the callee's
    # precondition (valid_range -> __CPROVER_r_ok) are ASSERTED at the call site so
    # caller misuse is CAUGHT; structural clauses stay assumed (avoids assert-mode
    # false alarms). Real-code only -- SV-COMP untouched. scope_from_entry/frame_havoc
    # inline the callee cone, so they don't stub-and-need this.
    if plan.strategy == "compositional" and not os.environ.get("BMC_SVCOMP_MODE"):
        os.environ["BMC_ASSERT_BOUNDS_ONLY"] = "1"
    else:
        os.environ.pop("BMC_ASSERT_BOUNDS_ONLY", None)
    only = set(plan.targets) if plan.targets else None
    logger.info("apply_plan: %s", plan.summary())
    return only
