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

        # Witness-pattern pre-check: certain CBMC nondet defaults indicate
        # the bug requires the library to be uninitialized. Skip the LLM
        # and return UNREALISTIC directly. Saves an LLM round-trip AND
        # avoids the realism LLM "creatively" justifying these artifacts.
        artifact_cause = _witness_indicates_uninitialized_library(counterexample)
        if artifact_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by witness-pattern — %s",
                func.name, artifact_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"Witness-pattern auto-classification: {artifact_cause}. "
                    "Skipped LLM realism call because this CBMC default state "
                    "(library globals NULL) cannot occur after standard "
                    "library init runs."
                ),
                key_concern=f"[witness-artifact] {artifact_cause}",
                llm_confidence="high",
            )

        # jq jv tagged-union stub-disconnect detector (arm-(a), 2026-05-13,
        # from jv_aux.c sweep): CBMC's nondet `jv` struct has u.ptr = NULL
        # while stubbed `jv_get_kind` returns refcnt-backed kinds (ARRAY/
        # STRING/OBJECT/NUMBER) or out-of-range enum integers. The
        # implementation then dereferences the NULL refcnt — an artifact
        # of stub disconnect, not a real bug.
        jv_cause = _witness_indicates_jv_stub_disconnect(counterexample)
        if jv_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by jv stub-disconnect — %s",
                func.name, jv_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"jq jv tagged-union stub-disconnect: {jv_cause}. "
                    "Real jq code obtains jv values via constructors "
                    "(jv_array, jv_string, jv_object, …) which always pair "
                    "the kind with a valid refcnt. The CBMC nondet jv has "
                    "u.ptr=NULL while the stubbed jv_get_kind reports a "
                    "refcnt-backed kind — an impossible runtime state."
                ),
                key_concern=f"[jv-stub-disconnect] {jv_cause}",
                llm_confidence="high",
            )

        # Path-divergent unwind detector (from feedback-loop arm (a) TODO,
        # 2026-05-13): CBMC reports a *.unwind.* property fail when its
        # symbolic exploration on SOME path hits the loop bound; the
        # reported counterexample witness, however, may correspond to a
        # different path that exits early without ever entering the loop.
        # When the witness shows function-return before any loop-head,
        # the violation is path-divergent — not a bug along the witnessed
        # path. Skip the LLM call.
        divergent_cause = _witness_indicates_path_divergent_unwind(counterexample)
        if divergent_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by path-divergent "
                "unwind — %s",
                func.name, divergent_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"Path-divergent unwind artifact: {divergent_cause}. "
                    "CBMC's unwind assertion fires on a symbolic path that "
                    "the exhibited witness doesn't actually traverse. The "
                    "exhibited execution exits before reaching the loop "
                    "whose bound was exceeded."
                ),
                key_concern=f"[path-divergent-unwind] {divergent_cause}",
                llm_confidence="high",
            )

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
            # max_tokens=2048 caused realism responses to truncate mid-JSON for
            # complex findings (observed in libxml2 xmlAddEntity); the parser
            # then fell back to prose-keyword recovery and picked the wrong
            # verdict. Bump to 4096; with thinking enabled the SDK auto-expands
            # to thinking_budget + 1024, so we override that calculation here.
            raw = self.llm.complete(
                SPEC_SYSTEM_PROMPT,
                prompt,
                max_tokens=4096 + (4000 if use_thinking else 0),
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
        violated = _format_failing_property_with_location(counterexample)
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
        # Mark the failing line with `>>>` arrow so the LLM can locate
        # the property without manually mapping CBMC's instruction index.
        failure_line = None
        loc = getattr(counterexample, "failure_location", None) or {}
        if isinstance(loc, dict):
            try:
                failure_line = int(loc.get("line", "")) if loc.get("line") else None
            except (TypeError, ValueError):
                failure_line = None
        source_file_context = _format_source_file_context(parsed_file, mark_line=failure_line)

        tm = getattr(self.config, "threat_model", "security")
        return REALISM_CHECK_PROMPT.format(
            threat_model_context=THREAT_MODEL_CONTEXT.get(tm, THREAT_MODEL_CONTEXT["security"]),
            function_name=func.name,
            function_signature=sig,
            # 8000 chars (~120 lines of C) accommodates mid-size leaf functions
            # like libxml2 xmlAddEntity (~110 lines) without truncating past the
            # lazy-init / guard region that the LLM needs to see.
            function_body=body[:8000],
            violated_property=violated,
            counterexample_state=cex_state,
            call_chain=call_chain_str,
            caller_context=caller_context,
            dynamic_result=dynamic_result,
            harness_code=harness_code,
            call_site_analysis=call_site_analysis,
            global_context=global_context,
            source_file_context=source_file_context,
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


_LIB_INIT_GLOBAL_FN_PTRS = {
    # libxml2 global allocator / dict / debug pointers
    "xmlMalloc", "xmlMallocAtomic", "xmlRealloc", "xmlFree", "xmlMemStrdup",
    "xmlGenericError", "xmlStructuredError", "xmlOutputBufferCreateFilenameValue",
    "xmlParserInputBufferCreateFilenameValue",
    # libcurl indirect allocators
    "Curl_cmalloc", "Curl_ccalloc", "Curl_crealloc", "Curl_cfree", "Curl_cstrdup",
    "Curl_cscalloc",
    # OpenSSL / BoringSSL function-pointer indirections
    "OPENSSL_malloc", "OPENSSL_zalloc", "OPENSSL_free", "OPENSSL_realloc",
    "CRYPTO_malloc", "CRYPTO_free", "CRYPTO_realloc",
    # glib
    "g_malloc", "g_free", "g_realloc",
}


def _witness_indicates_uninitialized_library(cex: "Counterexample") -> str | None:
    """Return a one-line description of the artifact pattern if the
    counterexample's variable assignments indicate the witness only
    triggers when library-init has not run (CBMC nondet default = NULL
    for unset global function pointers). Otherwise None.

    Real public APIs always go through ``xmlInit`` / ``curl_global_init`` /
    ``OPENSSL_init_crypto`` / ``g_thread_init`` etc. at startup, which set
    these globals; a CEx that needs them NULL is an artifact, not a bug.
    """
    if not cex or not getattr(cex, "variable_assignments", None):
        return None
    nulled: list[str] = []
    for var, val in cex.variable_assignments.items():
        if var in _LIB_INIT_GLOBAL_FN_PTRS:
            # CBMC formats NULL as "NULL" or "(... *)NULL" / "((xmlMallocFunc)NULL)"
            val_str = (val or "").upper()
            if "NULL" in val_str and "!=" not in val_str:
                nulled.append(var)
    if len(nulled) >= 2:
        return (
            f"witness requires {len(nulled)} library-init global function pointers "
            f"to be NULL ({', '.join(sorted(nulled)[:4])}"
            f"{', …' if len(nulled) > 4 else ''}), "
            "which only happens before library init runs"
        )
    return None


_JV_REFCNT_KINDS = {"JV_KIND_STRING", "JV_KIND_ARRAY", "JV_KIND_OBJECT", "JV_KIND_NUMBER"}
_JV_VALID_KINDS = {
    "JV_KIND_INVALID", "JV_KIND_NULL", "JV_KIND_FALSE", "JV_KIND_TRUE",
    "JV_KIND_NUMBER", "JV_KIND_STRING", "JV_KIND_ARRAY", "JV_KIND_OBJECT",
}


def _witness_indicates_jv_stub_disconnect(cex: "Counterexample") -> str | None:
    """Detect jq's jv tagged-union stub-disconnect pattern.

    jq represents values as a tagged-union struct ``jv = { kind_flags,
    pad_, offset, size, u: { ptr | number } }`` where refcnt-backed kinds
    (STRING/ARRAY/OBJECT/NUMBER-large) require ``u.ptr`` to be a valid
    ``jv_refcnt *``. Real jq code obtains jv values only via constructors
    (jv_array, jv_string, jv_object, …) which always pair the kind with
    a valid refcnt.

    CBMC, given a nondet ``jv`` parameter and a STUB ``jv_get_kind`` that
    returns nondet enum, can produce a witness where ``j.u.ptr == NULL``
    AND ``jv_get_kind(j)`` returns a refcnt-backed kind. The implementation
    then dereferences NULL — a model artifact, not a real bug.

    Trigger when:
      (a) ≥1 ``*.u.ptr`` assignments are NULL, AND
      (b) ≥1 ``return_value_jv_get_kind*`` assignments are either a
          refcnt-backed kind or an out-of-enum-range integer
          (e.g. ``/*enum*/17``, ``/*enum*/2097152``).
    """
    if not cex or not getattr(cex, "variable_assignments", None):
        return None
    null_ptr_count = 0
    suspect_kinds: list[str] = []
    for var, val in cex.variable_assignments.items():
        v = (val or "")
        # u.ptr fields that are NULL
        if var.endswith(".u.ptr") or var.endswith(".ptr"):
            if "NULL" in v.upper():
                null_ptr_count += 1
        # jv_get_kind stub returns
        if var.startswith("return_value_jv_get_kind"):
            # CBMC writes "/*enum*/JV_KIND_xxx" or "/*enum*/<int>"
            m = re.search(r"/\*enum\*/(\S+)", v)
            if not m:
                continue
            tok = m.group(1).strip().rstrip(",;")
            if tok in _JV_REFCNT_KINDS:
                suspect_kinds.append(tok)
            elif tok.isdigit() or (tok.startswith("-") and tok[1:].isdigit()):
                # Out-of-range enum integer — bogus stub return
                try:
                    n = int(tok)
                    if n < 0 or n > 7:
                        suspect_kinds.append(f"int({n})")
                except ValueError:
                    pass
            elif tok not in _JV_VALID_KINDS:
                # Unknown enum tag (e.g. "/*enum*/42_FOO") — also suspect
                suspect_kinds.append(tok)
    if null_ptr_count >= 1 and len(suspect_kinds) >= 1:
        sample = ", ".join(suspect_kinds[:3])
        more = "" if len(suspect_kinds) <= 3 else f" (+{len(suspect_kinds)-3} more)"
        return (
            f"{null_ptr_count} jv.u.ptr field(s) are NULL while stubbed "
            f"jv_get_kind reports refcnt-backed/out-of-range kinds "
            f"[{sample}{more}] — real jv constructors never pair these"
        )
    return None


def _witness_indicates_path_divergent_unwind(cex: "Counterexample") -> str | None:
    """Return a description of the divergence if the failing property is
    `*.unwind.*` but the trace shows the function returning early
    (before any loop-head). Otherwise None.

    Detection from feedback-loop arm (a) TODO #1 (2026-05-13,
    xmlXIncludeIncludeNode): CBMC's unwinding-assertion machinery emits
    `*.unwind.N` when ANY symbolic path needs more iterations than the
    bound, even when the EXHIBITED counterexample witness corresponds
    to a path that takes an early-exit branch and never enters the loop.
    These are pure path-divergence artifacts.
    """
    prop = (cex.failing_property or "")
    if ".unwind." not in prop:
        return None
    trace = cex.trace or []
    if not trace:
        return None
    # Walk the trace; find the first loop-head and the first function-return.
    first_loop_head = None
    first_func_return = None
    for i, step in enumerate(trace):
        s = (step or "").lower()
        if first_loop_head is None and "loop-head" in s:
            first_loop_head = i
        if first_func_return is None and ("function-return" in s or s.startswith("return_value")):
            first_func_return = i
    # If we saw an early return AND it's before any loop-head, the
    # exhibited path never iterates → unwind property fires on a different
    # path. (If the function has no loops at all, first_loop_head is None;
    # in that case the .unwind.* property is also a divergence artifact.)
    if first_func_return is not None and (
        first_loop_head is None or first_func_return < first_loop_head
    ):
        if first_loop_head is None:
            return (
                f"property '{prop}' fires but the function body has no loop on the "
                "exhibited path (witness exits without entering any loop)"
            )
        return (
            f"property '{prop}' fires on a non-exhibited path; the witness "
            "returns at trace step %d before reaching any loop-head (first "
            "loop-head at step %d)" % (first_func_return, first_loop_head)
        )
    return None


def _format_failing_property_with_location(cex: "Counterexample") -> str:
    """Format the failing property with file:line + description.

    CBMC reports e.g. ``func.pointer_dereference.47``; the LLM has no way
    to know which line that maps to. Append the source location and
    one-line description so the LLM points at concrete code instead of
    guessing instruction offsets.
    """
    prop = cex.failing_property or "<unknown>"
    parts = [prop]
    desc = getattr(cex, "description", "") or ""
    if desc:
        parts.append(f"({desc})")
    loc = getattr(cex, "failure_location", None) or {}
    if isinstance(loc, dict) and loc:
        file_ = loc.get("file", "?")
        line = loc.get("line", "?")
        func = loc.get("function", "")
        loc_str = f"at {file_}:{line}"
        if func:
            loc_str += f" in {func}"
        parts.append(loc_str)
    return " ".join(parts)


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


def _gather_sibling_sources(source_path: str, max_total_bytes: int = 60_000) -> str:
    """Return concatenated bodies of OTHER .c files in the same directory
    as the source under test, capped at ``max_total_bytes``.

    Without this, the realism LLM can't see the bodies of project-internal
    helpers like jq's ``jv_mem_realloc`` (in ``jv_alloc.c``) that are
    called from ``jv_parse.c``. With it visible, the LLM can determine
    whether the callee aborts on failure (never returns NULL) vs returns
    a value the caller must check.
    """
    import os
    if not source_path:
        return ""
    try:
        src_dir = os.path.dirname(os.path.abspath(source_path))
        my_base = os.path.basename(source_path)
    except Exception:
        return ""
    if not os.path.isdir(src_dir):
        return ""
    parts: list[str] = []
    total = 0
    try:
        entries = sorted(os.listdir(src_dir))
    except Exception:
        return ""
    for name in entries:
        if not name.endswith(".c"):
            continue
        if name == my_base:
            continue
        try:
            with open(os.path.join(src_dir, name), "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
        except Exception:
            continue
        section = f"\n/* === SIBLING SOURCE: {name} === */\n{body}"
        if total + len(section) > max_total_bytes:
            # Truncate this file to fit
            remaining = max_total_bytes - total
            if remaining > 5000:
                section = section[:remaining] + "\n/* ... truncated ... */"
                parts.append(section)
                total += len(section)
            break
        parts.append(section)
        total += len(section)
    return "".join(parts)


def _gather_relevant_headers(source_path: str, max_total_bytes: int = 80_000) -> str:
    """Walk `#include "foo.h"` directives in the source file and try to
    locate the referenced headers in sibling directories. Returns the
    concatenated header bodies, capped at ``max_total_bytes`` so the
    realism prompt budget stays under control.

    Used to surface struct/typedef/macro definitions the LLM needs to
    reason about (xmlXIncludeCtxt fields, xmlPattern layout, etc.) that
    live in adjacent .h files rather than the .c file we're verifying.

    Quoted includes only (``#include "foo.h"``) — angle-bracket
    includes are system headers that bloat the prompt without help.
    """
    import os
    import re
    try:
        with open(source_path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except Exception:
        return ""
    headers_referenced: list[str] = []
    for m in re.finditer(r'#\s*include\s+"([^"]+)"', src):
        headers_referenced.append(m.group(1))
    if not headers_referenced:
        return ""
    # Search candidate locations: source dir, ./include, ../include,
    # and sibling include/<basename-no-ext>/ which matches the libxml2
    # / libssh2 layout.
    src_dir = os.path.dirname(os.path.abspath(source_path))
    search_dirs = [
        src_dir,
        os.path.join(src_dir, "include"),
        os.path.join(src_dir, "..", "include"),
    ]
    parts: list[str] = []
    total = 0
    seen: set[str] = set()
    for hdr in headers_referenced:
        if hdr in seen:
            continue
        seen.add(hdr)
        found_path = None
        for d in search_dirs:
            candidate = os.path.normpath(os.path.join(d, hdr))
            if os.path.exists(candidate):
                found_path = candidate
                break
            # Try basename of the include path under each search dir
            # (handles "libxml/xinclude.h" matched at include/libxml/xinclude.h).
            for root, _, files in os.walk(d, followlinks=False):
                bn = os.path.basename(hdr)
                if bn in files:
                    cand2 = os.path.join(root, bn)
                    if cand2.endswith(hdr) or bn == hdr:
                        found_path = cand2
                        break
                if found_path:
                    break
            if found_path:
                break
        if not found_path:
            continue
        try:
            with open(found_path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
        except Exception:
            continue
        # Skip pure-decl-only forward-only headers (no struct/typedef body)
        if "struct" not in body and "typedef" not in body and "enum" not in body:
            continue
        section = f"\n/* === HEADER: {hdr} (from {found_path}) === */\n{body}"
        if total + len(section) > max_total_bytes:
            break
        parts.append(section)
        total += len(section)
    return "".join(parts)


def _format_source_file_context(parsed_file: "ParsedCFile", mark_line: int | None = None) -> str:
    """Return the full source file body, lightly truncated, for the realism prompt.

    The LLM hallucinated missing guards in two libxml2 cases (xmlBufferEmpty
    and xmlAddEntity) because it didn't see the bodies of related functions
    in the same file (xmlBufferDetach lazy-nulls content; xmlAddEntity's
    lazy-init `dtd->entities = xmlHashCreate(...)` was past the
    function-body cut). Send the full file so the LLM can cross-check.

    Cap at 50k chars to stay within prompt budget; most libxml2 / curl /
    OpenSSL leaf-parser files are 2k-30k chars.
    """
    src = getattr(parsed_file, "preprocessed_source", None)
    if not src:
        # Read the original file from disk.
        try:
            import os as _os
            path = getattr(parsed_file, "path", "")
            if path and _os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
        except Exception:
            src = None
    if not src:
        # Fall back: concatenate per-function bodies.
        bodies = getattr(parsed_file, "function_bodies", None) or {}
        parts: list[str] = []
        for fname, body in bodies.items():
            if body:
                parts.append(f"/* --- {fname} --- */\n{body}")
        src = "\n\n".join(parts)
    if not src:
        return "(source file context not available)"
    # Optionally annotate the failing line. We number lines so the LLM can
    # cross-reference against the file:line shown in the failing-property
    # block, and prefix the suspected line with `>>>` so it stands out.
    if mark_line is not None:
        numbered_lines: list[str] = []
        for idx, line in enumerate(src.splitlines(), start=1):
            marker = ">>>" if idx == mark_line else "   "
            numbered_lines.append(f"{idx:5d} {marker} {line}")
        src = "\n".join(numbered_lines)

    # Cap at 200 KB. Empirically: a 50 KB cap caused UNCERTAIN verdicts
    # on libxml2 xinclude.c (~80 KB) when the LLM needed to inspect a
    # callee's body that fell past the cut. The realism prompt's other
    # sections add ~10-15 KB; sonnet-4-6's context window comfortably
    # accommodates 200 KB of source for the typical leaf-parser file
    # (largest libxml2 file is parser.c at ~280 KB, still fine with
    # boundary truncation).
    # Append relevant header bodies. Struct/typedef definitions live in
    # adjacent .h files; without them the realism LLM has to guess
    # field types and produces UNCERTAIN verdicts where it could be
    # decisive.
    hdr_src = _gather_relevant_headers(getattr(parsed_file, "path", "") or "")
    if hdr_src:
        src = src + "\n\n/* ===== Adjacent header bodies for type/struct context ===== */\n" + hdr_src

    # Append sibling .c files in the same directory. Without these, the
    # realism LLM can't see helper functions like jq's ``jv_mem_realloc``
    # (defined in jv_alloc.c) called from jv_parse.c — it then can't
    # determine whether the helper aborts on failure or returns NULL,
    # producing UNCERTAIN verdicts that should be decisive.
    sib_src = _gather_sibling_sources(getattr(parsed_file, "path", "") or "")
    if sib_src:
        src = src + "\n\n/* ===== Sibling .c source bodies for helper-function context ===== */\n" + sib_src

    cap = 200_000
    if len(src) <= cap:
        return src
    # Truncate at a function boundary if possible.
    head = src[:cap]
    last_close = head.rfind("\n}")
    if last_close > cap - 5000:
        head = head[: last_close + 2]
    return head + "\n\n/* ... rest of file truncated for prompt budget ... */"


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

    Looks for an explicit verdict marker in priority order:

      1. JSON-style key ``"verdict": "X"`` (matches even when the surrounding
         JSON is truncated and can't be parsed by ``json.loads``).
      2. Prose anchors like ``Verdict: X`` / ``**Verdict** — X`` /
         ``Final verdict = X``.
      3. Bare-keyword fallback, but ONLY when exactly one of the verdict
         words appears anywhere in the text. If multiple appear without
         a clear anchor, default to UNCERTAIN — the LLM was hedging or
         used "realistic" inside reasoning prose.

    Order of alternation puts ``UNREALISTIC`` first so the regex engine
    doesn't greedily match ``REALISTIC`` as a prefix.
    """
    import re as _re
    upper = text.upper()

    # 1. JSON key form: "verdict": "X" or 'verdict': 'X'
    m = _re.search(
        r'"VERDICT"\s*:\s*"(UNREALISTIC|REALISTIC|UNCERTAIN)"',
        upper,
    )
    if m:
        token = m.group(1)
    else:
        # 2. Prose anchor: Verdict: X / Verdict — X / Verdict = X / **Verdict**: X
        m = _re.search(
            r"\bVERDICT\b[\s\*]*[:\-—=]\s*\"?(UNREALISTIC|REALISTIC|UNCERTAIN)\b",
            upper,
        )
        if m:
            token = m.group(1)
        else:
            # 3. Bare-keyword fallback, conservatively. If more than one
            # verdict word appears without an anchor, the LLM probably
            # used the words inside reasoning text — don't guess.
            words_present = [
                w for w in ("UNREALISTIC", "REALISTIC", "UNCERTAIN")
                if w in upper
            ]
            # REALISTIC is a substring of UNREALISTIC; if both are
            # 'present' but it's really just UNREALISTIC, dedup.
            if "UNREALISTIC" in words_present and "REALISTIC" in words_present:
                # Count standalone REALISTIC occurrences (not preceded by 'UN')
                standalone = len(_re.findall(r"(?<!UN)\bREALISTIC\b", upper))
                if standalone == 0:
                    words_present.remove("REALISTIC")
            if len(words_present) == 1:
                token = words_present[0]
            elif len(words_present) > 1:
                # Hedge — keep but mark UNCERTAIN.
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

    # New evidence fields the prompt now requires for REALISTIC verdicts.
    source_line_guard = str(data.get("source_line_guard", "")).strip()
    public_api_call_chain = str(data.get("public_api_call_chain", "")).strip()

    # Consistency checks: this session's bounty runs surfaced two patterns
    # where the LLM's verdict field comes back REALISTIC but the verdict
    # is not supported by the reasoning. Trust the evidence over the label
    # and downgrade in two cases.
    #
    # Case 1: Reasoning text explicitly identifies the finding as an
    # artifact / stub behavior / harness simplification. Examples seen:
    # ASN1_STRING_type_new (reasoning noted `if (ret == NULL) return NULL;`
    # guard); curl_url_dup (`Curl_ccalloc=NULL is a CBMC stub artifact`);
    # checktz / datestring (`bsearch return treated as unconstrained`).
    #
    # Case 2: REALISTIC verdict but the LLM failed to populate the
    # REQ-1 source-line guard analysis OR REQ-2 public-API call chain.
    # These are mandatory evidence per the prompt; an empty / "no guard
    # found" / "cannot construct" answer means the LLM didn't actually
    # demonstrate the bug is reachable from a real entry point.
    if verdict == RealismVerdict.REALISTIC:
        downgrade_reason = _detect_artifact_phrases(
            reasoning + " " + key_concern
            + " " + source_line_guard + " " + public_api_call_chain
        )
        if downgrade_reason:
            logger.warning(
                "Realism check: downgrading REALISTIC → UNREALISTIC for '%s' "
                "(matched '%s' — likely CBMC modelling artifact)",
                func_name, downgrade_reason,
            )
            verdict = RealismVerdict.UNREALISTIC
            tag = f"[auto-downgraded: matched '{downgrade_reason}']"
            key_concern = (tag + " " + key_concern) if key_concern else tag
        elif _is_evidence_missing(source_line_guard, public_api_call_chain):
            logger.warning(
                "Realism check: downgrading REALISTIC → UNCERTAIN for '%s' "
                "(missing source-line guard or public-API call chain)",
                func_name,
            )
            verdict = RealismVerdict.UNCERTAIN
            tag = (
                "[auto-downgraded: REALISTIC verdict without REQ-1 "
                "source-line guard analysis or REQ-2 public-API call chain]"
            )
            key_concern = (tag + " " + key_concern) if key_concern else tag

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


# Phrases that strongly indicate the reasoning concluded the finding is
# a harness / CBMC modelling artifact rather than a real bug. When any
# of these appears in the reasoning text, the verdict is downgraded
# to UNREALISTIC regardless of what the LLM labelled.
_ARTIFACT_PHRASES = [
    "cbmc modelling artifact", "cbmc modeling artifact",
    "cbmc symbolic artifact", "symbolic artifact",
    "cbmc stub", "stub returns", "stubbed extern",
    "harness has a simplified", "harness modelling",
    "harness simplification", "the harness models",
    "harness allocates", "harness under-bounds",
    "unconstrained by cbmc", "unconstrained pointer",
    "treated as unconstrained",
    "specific witness is unrealistic",
    "specific witness requires", "witness value is",
    "is a cbmc artifact",
    "the actual function body has guards",
    "real code has", "real curl", "real openssl", "real nghttp2",
    "the if check", "the null check", "is guarded by",
]


def _detect_artifact_phrases(text: str) -> str | None:
    """Return the matched artifact phrase, or None if reasoning looks like
    a genuine bug claim. Case-insensitive substring search."""
    if not text:
        return None
    lowered = text.lower()
    for phrase in _ARTIFACT_PHRASES:
        if phrase in lowered:
            return phrase
    return None


# Phrases that indicate the LLM didn't actually fulfill the
# REQ-1 / REQ-2 evidence requirements and is hand-waving.
_EVIDENCE_MISSING_PHRASES = [
    "no guard found", "no explicit guard", "no null check",
    "cannot construct", "cannot produce a", "unable to construct",
    "no concrete chain", "no public api", "not exposed",
    "this is hypothetical", "in theory", "theoretically",
    "any caller could", "an attacker could", "if a caller",
]


def _is_evidence_missing(source_line_guard: str, public_api_call_chain: str) -> bool:
    """REALISTIC verdicts must include concrete REQ-1 and REQ-2 evidence.
    Treat empty / hand-wave answers as missing evidence."""
    def _empty_or_handwave(s: str) -> bool:
        if not s or len(s.strip()) < 20:
            return True
        lowered = s.lower()
        return any(p in lowered for p in _EVIDENCE_MISSING_PHRASES)

    return _empty_or_handwave(source_line_guard) or _empty_or_handwave(public_api_call_chain)
