"""
Phase 3 Post-Validation: Realism Checker [AGENTIC].

LLM agent that audits every REAL_BUG finding for realistic exploitability.
Reduces false positives by asking: could real program execution actually
produce the counterexample's input state?

Verdicts
--------
REALISTIC   — finding is plausible; confidence tier is unchanged
UNREALISTIC — finding requires inputs impossible in the real program;
              confidence tier is downgraded to "unlikely"
UNCERTAIN   — cannot determine; finding is kept but annotated

The checker runs after Phase 3 classification but before the BugReport is
committed.  It is skipped when config.enable_realism_check is False (default),
so it never silently changes existing behaviour without opt-in.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

from bmc_agent.logger import get_logger
from bmc_agent.prompts import REALISM_CHECK_PROMPT, SPEC_SYSTEM_PROMPT, THREAT_MODEL_CONTEXT

if TYPE_CHECKING:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import ValidationResult
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient, LLMError
    from bmc_agent.parser import FunctionInfo, ParsedCFile
    from bmc_agent.spec import Spec

logger = get_logger("realism_checker")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class RealismVerdict(str, Enum):
    REALISTIC   = "realistic"
    UNREALISTIC = "unrealistic"
    UNCERTAIN   = "uncertain"


@dataclass
class RealismCheckResult:
    verdict: RealismVerdict
    reasoning: str
    key_concern: str = ""      # populated when verdict is UNREALISTIC or UNCERTAIN
    llm_confidence: str = ""   # "high" | "medium" | "low" from the LLM

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reasoning": self.reasoning,
            "key_concern": self.key_concern,
            "llm_confidence": self.llm_confidence,
        }


# Default result when the checker is skipped or fails.
_SKIPPED = RealismCheckResult(
    verdict=RealismVerdict.UNCERTAIN,
    reasoning="Realism check skipped.",
)


# ---------------------------------------------------------------------------
# RealismChecker
# ---------------------------------------------------------------------------


class RealismChecker:
    """
    LLM agent that audits REAL_BUG findings for realistic exploitability.

    Parameters
    ----------
    config : Config
        Pipeline configuration.  `config.enable_realism_check` must be True
        for the checker to run.
    llm : LLMClient
        Shared LLM client.
    """

    def __init__(self, config: "Config", llm: "LLMClient") -> None:
        self.config = config
        self.llm = llm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        func: "FunctionInfo",
        counterexample: "Counterexample",
        validation_result: "ValidationResult",
        parsed_file: "ParsedCFile",
        all_funcs: "dict[str, FunctionInfo]",
        spec: "Spec",
    ) -> RealismCheckResult:
        """
        Audit a REAL_BUG finding for realistic exploitability.

        Returns a RealismCheckResult.  When `config.enable_realism_check` is
        False the result is UNCERTAIN/skipped so the caller's behaviour is
        unchanged.
        """
        from bmc_agent.llm import LLMError  # local import to avoid circular

        if not self.config.enable_realism_check:
            return _SKIPPED

        logger.info("Realism check for '%s' (property: %s)",
                    func.name, counterexample.failing_property[:60])

        prompt = self._build_prompt(
            func=func,
            counterexample=counterexample,
            validation_result=validation_result,
            parsed_file=parsed_file,
            all_funcs=all_funcs,
        )

        try:
            # Use extended thinking when available to improve reasoning quality.
            use_thinking = getattr(self.config, "enable_realism_thinking", False)
            raw = self.llm.complete(
                SPEC_SYSTEM_PROMPT,
                prompt,
                max_tokens=2048,
                thinking=use_thinking,
                thinking_budget=4000,
            )
            return _parse_result(raw, func.name)
        except LLMError as exc:
            logger.warning("Realism check LLM call failed for '%s': %s", func.name, exc)
            return RealismCheckResult(
                verdict=RealismVerdict.UNCERTAIN,
                reasoning=f"LLM call failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        func: "FunctionInfo",
        counterexample: "Counterexample",
        validation_result: "ValidationResult",
        parsed_file: "ParsedCFile",
        all_funcs: "dict[str, FunctionInfo]",
    ) -> str:
        sig = _format_signature(func)
        body = func.body or "(body not available)"
        violated = counterexample.failing_property
        cex_state = _format_cex_state(counterexample)
        call_chain_str = " → ".join(validation_result.caller_path) if validation_result.caller_path else func.name
        caller_context = _format_caller_context(
            validation_result.caller_path, all_funcs, parsed_file
        )
        dynamic_result = _format_dynamic_result(validation_result)
        harness_code = _format_harness_code(validation_result)

        call_site_analysis = _format_call_site_analysis(
            func.name, validation_result.caller_path, all_funcs, parsed_file
        )
        global_context = _extract_global_context(func, all_funcs, parsed_file)

        tm = getattr(self.config, "threat_model", "security")
        return REALISM_CHECK_PROMPT.format(
            threat_model_context=THREAT_MODEL_CONTEXT.get(tm, THREAT_MODEL_CONTEXT["security"]),
            function_name=func.name,
            function_signature=sig,
            function_body=body[:2000],
            violated_property=violated,
            counterexample_state=cex_state,
            call_chain=call_chain_str,
            caller_context=caller_context,
            dynamic_result=dynamic_result,
            harness_code=harness_code,
            call_site_analysis=call_site_analysis,
            global_context=global_context,
        )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _extract_call_sites(
    func_name: str,
    all_funcs: "dict[str, FunctionInfo]",
    parsed_file: "ParsedCFile",
) -> list[tuple[str, str]]:
    """Find call sites of func_name in other function bodies. Returns (caller, snippet) pairs."""
    pattern = re.compile(
        r"(?<![a-zA-Z0-9_])" + re.escape(func_name) + r"\s*\([^;]{0,200}",
        re.DOTALL,
    )
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    all_bodies: dict[str, str] = {}
    for fname, finfo in (all_funcs or {}).items():
        if fname != func_name and finfo.body:
            all_bodies[fname] = finfo.body
    for fname in list((parsed_file.functions if parsed_file.functions else {}).keys()):
        if fname != func_name and fname not in all_bodies:
            fi = parsed_file.get_function_info(fname)
            if fi and fi.body:
                all_bodies[fname] = fi.body

    for caller_name, body in all_bodies.items():
        for m in pattern.finditer(body):
            snippet = m.group(0).strip()[:160].replace("\n", " ")
            key = f"{caller_name}:{snippet[:40]}"
            if key not in seen:
                seen.add(key)
                results.append((caller_name, snippet))
            if len(results) >= 8:
                return results
    return results


def _is_likely_dead_code(
    func_name: str,
    all_funcs: "dict[str, FunctionInfo]",
    parsed_file: "ParsedCFile",
) -> bool:
    """True if func_name has no call sites and no function-pointer references in this file."""
    # Check parsed_file call graph
    for callees in (parsed_file.call_graph or {}).values():
        if func_name in callees:
            return False
    # Check all_funcs call graphs and bodies
    fp_pattern = re.compile(r"(?<![a-zA-Z0-9_])" + re.escape(func_name) + r"(?!\s*\()")
    for finfo in (all_funcs or {}).values():
        if func_name in finfo.callees:
            return False
        if finfo.body and fp_pattern.search(finfo.body):
            return False
    return True


def _extract_global_context(
    func: "FunctionInfo",
    all_funcs: "dict[str, FunctionInfo]",
    parsed_file: "ParsedCFile",
) -> str:
    """
    For global variables referenced in func.body, find where they are assigned
    in the codebase and return a summary of those assignment patterns.
    """
    if not func.body:
        return "(function body unavailable)"

    # Heuristic: find bare identifiers that look like globals (not local decls, not params).
    # Look for simple_name = ... assignment patterns in other function bodies.
    param_names = {pn for _, pn in func.signature.parameters if pn}

    # Collect potential global names: identifiers that appear in the body but aren't params.
    # Focus on names that appear on the left-hand side of assignments elsewhere.
    global_assign_pattern = re.compile(
        r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\[[^\]]*\])?\s*=\s*([^=;,\n]{1,80})"
    )

    all_bodies: dict[str, str] = {}
    for fname, finfo in (all_funcs or {}).items():
        if fname != func.name and finfo.body:
            all_bodies[fname] = finfo.body
    for fname in list((parsed_file.functions if parsed_file.functions else {}).keys()):
        if fname != func.name and fname not in all_bodies:
            fi = parsed_file.get_function_info(fname)
            if fi and fi.body:
                all_bodies[fname] = fi.body

    # Find identifiers in func body that are referenced but not params/locals
    # Simple: look for words in func body that also appear as assignment LHS elsewhere
    body_words = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\b", func.body))
    body_words -= param_names
    body_words -= {"NULL", "true", "false", "sizeof", "return", "if", "else",
                   "while", "for", "int", "char", "void", "uint32_t", "uint8_t",
                   "uint64_t", "static", "const", "struct", "unsigned"}

    assignments: dict[str, list[str]] = {}
    for caller_name, body in all_bodies.items():
        for m in global_assign_pattern.finditer(body):
            lhs = m.group(1)
            rhs = m.group(2).strip().rstrip(",;").strip()
            if lhs in body_words and len(rhs) < 80:
                assignments.setdefault(lhs, []).append(f"{lhs} = {rhs}  (in {caller_name})")

    if not assignments:
        return "(no global variable assignments found for variables used in this function)"

    lines: list[str] = []
    for var, assigns in sorted(assignments.items()):
        lines.append(f"  {var}:")
        for a in assigns[:3]:  # max 3 examples per variable
            lines.append(f"    {a}")
    return "\n".join(lines[:40])  # cap total output


def _format_call_site_analysis(
    func_name: str,
    caller_path: "list[str]",
    all_funcs: "dict[str, FunctionInfo]",
    parsed_file: "ParsedCFile",
) -> str:
    """Format call-site evidence and dead-code warning for the realism prompt."""
    call_sites = _extract_call_sites(func_name, all_funcs, parsed_file)
    dead = (not caller_path) and _is_likely_dead_code(func_name, all_funcs, parsed_file)

    parts: list[str] = []
    if dead:
        parts.append(
            "⚠ DEAD CODE: No call sites found for this function anywhere in this source "
            "file, and it is not referenced as a function pointer. The function may be "
            "unreachable dead code. Counterexample inputs are fully unconstrained and "
            "unlikely to reflect any real execution — treat as LOW CONFIDENCE."
        )
    if call_sites:
        parts.append("Actual call sites in this codebase:")
        for caller, snippet in call_sites:
            parts.append(f"  [{caller}] {snippet}")
    elif not dead:
        parts.append(
            "No call sites found in this file — this may be a public API entry point "
            "callable from external code. Inputs are unconstrained."
        )
    return "\n".join(parts) if parts else "(call-site analysis not available)"


def _format_signature(func: "FunctionInfo") -> str:
    sig = func.signature
    params = ", ".join(f"{pt} {pn}".strip() for pt, pn in sig.parameters)
    return f"{sig.return_type} {sig.name}({params})"


def _format_cex_state(cex: "Counterexample") -> str:
    if not cex.variable_assignments:
        return "(no witness values recorded)"
    lines = [f"  {k} = {v}" for k, v in cex.variable_assignments.items()]
    return "\n".join(lines)


def _format_caller_context(
    call_chain: "list[str]",
    all_funcs: "dict[str, FunctionInfo]",
    parsed_file: "ParsedCFile",
) -> str:
    if not call_chain or len(call_chain) <= 1:
        return "(no callers — function is a system entry point)"

    parts: list[str] = []
    # Show up to 3 callers from the chain (the direct callers nearest the buggy function)
    callers_to_show = call_chain[:-1][-3:]
    for caller_name in callers_to_show:
        fi = all_funcs.get(caller_name) or parsed_file.get_function_info(caller_name)
        if fi and fi.body:
            sig = f"{fi.signature.return_type} {fi.name}(...)"
            body_preview = fi.body[:800].rstrip()
            if len(fi.body) > 800:
                body_preview += "\n    /* ... */"
            parts.append(f"Caller `{sig}`:\n{body_preview}")
        else:
            parts.append(f"Caller `{caller_name}`: (body not available)")
    return "\n\n".join(parts) if parts else "(caller bodies not available)"


def _format_dynamic_result(vr: "ValidationResult") -> str:
    dyn = getattr(vr, "dynamic_result", None)
    if dyn is None:
        return "not run"
    outcome = dyn.outcome.value if hasattr(dyn.outcome, "value") else str(dyn.outcome)
    parts = [outcome]
    if dyn.signal_name:
        parts.append(f"signal={dyn.signal_name}")
    if dyn.reasoning:
        parts.append(dyn.reasoning[:200])
    return "; ".join(parts)


def _format_harness_code(vr: "ValidationResult") -> str:
    # system_entry_input is the LLM-generated reproducer or None
    reproducer = vr.system_entry_input
    # Dynamic harness source is stored on the DynamicValidationResult if available
    dyn = getattr(vr, "dynamic_result", None)
    harness_src = getattr(dyn, "harness_source", None) if dyn else None

    if harness_src:
        return harness_src[:1500]
    if reproducer:
        return reproducer[:1500]
    return "(harness code not available)"


def _extract_first_json_object(text: str) -> "str | None":
    """Find the first top-level ``{...}`` JSON object in *text*.

    The LLM sometimes wraps the expected object in surrounding prose
    ("Here is my analysis: { ... }").  Scan from the first ``{`` and
    return the substring covering its balanced match, or None if the
    braces are unbalanced.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _recover_verdict_from_prose(text: str) -> "RealismVerdict | None":
    """Best-effort verdict extraction from a non-JSON prose response.

    Looks for explicit verdict keywords. Order matters: ``UNREALISTIC``
    must be checked before ``REALISTIC`` (the latter is a substring of
    the former).
    """
    upper = text.upper()
    # Prefer a "Verdict: X" / "verdict": "X" / "**Verdict**: X" hit.
    import re as _re
    m = _re.search(
        r"VERDICT\s*[:\-=]?\s*\"?(UNREALISTIC|REALISTIC|UNCERTAIN)\b",
        upper,
    )
    if m:
        token = m.group(1)
    else:
        # Generic fallback: search for the keyword anywhere.
        if "UNREALISTIC" in upper:
            token = "UNREALISTIC"
        elif "REALISTIC" in upper:
            token = "REALISTIC"
        elif "UNCERTAIN" in upper:
            token = "UNCERTAIN"
        else:
            return None
    return {
        "REALISTIC":   RealismVerdict.REALISTIC,
        "UNREALISTIC": RealismVerdict.UNREALISTIC,
        "UNCERTAIN":   RealismVerdict.UNCERTAIN,
    }[token]


# ---------------------------------------------------------------------------
# LLM response parser
# ---------------------------------------------------------------------------


def _parse_result(raw: str, func_name: str) -> RealismCheckResult:
    text = raw.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.splitlines()
        inner: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not lines[0].startswith("```"):
                inner.append(line)
        text = "\n".join(inner).strip()

    # First try strict JSON.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback 1: extract a JSON object embedded in prose.
        embedded = _extract_first_json_object(text)
        if embedded is not None:
            try:
                data = json.loads(embedded)
            except json.JSONDecodeError:
                embedded = None
        if embedded is None:
            # Fallback 2: recover verdict + reasoning from prose. The LLM
            # often ignores "respond with ONLY valid JSON" and emits an
            # analytical paragraph that still contains the verdict word
            # somewhere. Better to recover an UNREALISTIC / REALISTIC
            # judgement from prose than to default everything to
            # UNCERTAIN with "Could not parse" — that hides real signal
            # downstream.
            recovered = _recover_verdict_from_prose(raw)
            if recovered is not None:
                logger.warning(
                    "Realism check: parsed verdict %s from prose for '%s' "
                    "(JSON failed)", recovered.value, func_name,
                )
                return RealismCheckResult(
                    verdict=recovered,
                    reasoning=raw.strip()[:2000],
                )
            logger.warning("Realism check: failed to parse JSON or recover verdict for '%s'", func_name)
            return RealismCheckResult(
                verdict=RealismVerdict.UNCERTAIN,
                reasoning=f"Could not parse LLM response: {raw[:200]}",
            )

    raw_verdict = str(data.get("verdict", "UNCERTAIN")).upper().strip()
    verdict_map = {
        "REALISTIC":   RealismVerdict.REALISTIC,
        "UNREALISTIC": RealismVerdict.UNREALISTIC,
        "UNCERTAIN":   RealismVerdict.UNCERTAIN,
    }
    verdict = verdict_map.get(raw_verdict, RealismVerdict.UNCERTAIN)
    reasoning = str(data.get("reasoning", "")).strip()
    key_concern = str(data.get("key_concern", "")).strip()
    llm_confidence = str(data.get("confidence", "")).strip()

    logger.info(
        "Realism check for '%s': verdict=%s confidence=%s",
        func_name, verdict.value, llm_confidence,
    )
    if verdict != RealismVerdict.REALISTIC and key_concern:
        logger.info("  Key concern: %s", key_concern[:150])

    return RealismCheckResult(
        verdict=verdict,
        reasoning=reasoning,
        key_concern=key_concern,
        llm_confidence=llm_confidence,
    )
