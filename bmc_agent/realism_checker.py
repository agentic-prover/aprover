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

        # NULL-guarded pointer-deref detector (from ch341 TODO #3,
        # 2026-05-18, ch341_reset_resume): the function body has an
        # explicit ``if (!ptr) return 0;`` early-return guard, but the
        # counterexample shows the guarded pointer as NULL and then
        # reports a deref violation at a later line. CBMC's symbolic
        # exploration didn't honour the path-condition imposed by the
        # guard (path-divergence inside the function). Reject pre-LLM:
        # the witness contradicts itself.
        guard_cause = _witness_indicates_null_guard_violation(func, counterexample)
        if guard_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by NULL-guard violation — %s",
                func.name, guard_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"NULL-guard early-return artifact: {guard_cause}. "
                    "The function body has an explicit guard that returns "
                    "before any further use of the pointer; a counterexample "
                    "showing the pointer NULL at a deref site contradicts "
                    "that guard's path condition. This is CBMC's symbolic "
                    "execution failing to prune the infeasible path."
                ),
                key_concern=f"[null-guard-violation] {guard_cause}",
                llm_confidence="high",
            )

        # USB-serial framework-invariant detector (from pl2303 finding,
        # 2026-05-18): the Linux USB-serial core wraps every driver
        # callback in a thin shim (``serial_tiocmset``, ``serial_open``,
        # …) that dereferences ``tty->driver_data`` (or ``port``)
        # *before* dispatching to the driver. The framework guarantees
        # the deref is safe by setting ``driver_data`` in
        # ``serial_install`` and gating any other op behind that install.
        # When CBMC explores a callback registered on a
        # ``struct usb_serial_driver`` dispatch table and the witness
        # shows the framework-set pointer as NULL, the framework's
        # wrapper would have crashed first — the deref is unreachable
        # along any framework-constructed path. Symmetric to the
        # library-init globals detector for a different framework class.
        usb_serial_cause = _witness_indicates_usb_serial_framework_invariant(
            func, counterexample, parsed_file
        )
        if usb_serial_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by USB-serial framework "
                "invariant — %s", func.name, usb_serial_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"USB-serial framework lifecycle invariant: {usb_serial_cause}. "
                    "The Linux USB-serial core's wrapper (serial_tiocmset, "
                    "serial_open, …) derefs tty->driver_data / port before "
                    "dispatching to the driver callback, so the framework "
                    "guarantees these pointers are non-NULL along any path "
                    "it can construct. A counterexample setting them NULL "
                    "violates the framework's install-time contract — the "
                    "wrapper would have crashed before reaching the callback."
                ),
                key_concern=f"[usb-serial-framework-invariant] {usb_serial_cause}",
                llm_confidence="high",
            )

        # PHY-framework-invariant detector (from dp83tc811_set_wol
        # finding, 2026-05-18): symmetric to the USB-serial detector for
        # ``struct phy_driver`` dispatch tables. When a callback
        # (``set_wol``, ``config_aneg``, ``handle_interrupt``, …) gets a
        # witness with ``phydev == NULL`` or
        # ``phydev->attached_dev == NULL``, the framework's attach
        # lifecycle (which sets both ``phydev->attached_dev = dev`` and
        # ``dev->phydev = phydev`` in the same conditional block, and
        # whose ethtool wrapper reaches the callback only via
        # ``dev->phydev``) rules out the NULL state along any
        # in-tree-reachable path.
        phy_cause = _witness_indicates_phy_framework_invariant(
            func, counterexample, parsed_file
        )
        if phy_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by PHY framework "
                "invariant — %s", func.name, phy_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"PHY framework lifecycle invariant: {phy_cause}. "
                    "Linux's phy_attach_direct sets phydev->attached_dev = "
                    "dev and dev->phydev = phydev in the same ``if (dev) "
                    "{ ... }`` block; phy_ethtool_set_wol and its sibling "
                    "wrappers reach the driver callback only via "
                    "dev->phydev, so attached_dev != NULL is a structural "
                    "invariant of every in-tree path that can dispatch "
                    "this callback."
                ),
                key_concern=f"[phy-framework-invariant] {phy_cause}",
                llm_confidence="high",
            )

        # Netdev-private framework-invariant detector (from rtl8125 OOT
        # finding batch, 2026-05-18): for any function in a driver file
        # that registers ``struct net_device_ops``, a witness setting
        # ``priv->{pci_dev,dev,netdev,pdev,mii_bus,mmio_addr}`` to NULL
        # contradicts the kernel's probe-time invariant — these back-
        # pointers are assigned in alloc_netdev/pci_probe before any
        # netdev callback can dispatch. Symmetric to the USB-serial and
        # PHY detectors for a third framework class. Excludes the probe
        # function itself, where these fields are legitimately
        # transitionally NULL.
        netdev_cause = _witness_indicates_netdev_private_framework_invariant(
            func, counterexample, parsed_file
        )
        if netdev_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by netdev-private "
                "framework invariant — %s", func.name, netdev_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"Netdev-private framework lifecycle invariant: "
                    f"{netdev_cause}. PCI/netdev drivers set the private "
                    "struct's back-pointers (pci_dev, dev/netdev, pdev) "
                    "during probe(), before alloc_netdev/register_netdev "
                    "makes the device visible to any of the registered "
                    "ndo_*/ethtool_ops callbacks. A counterexample with "
                    "these fields NULL violates the kernel's "
                    "probe-before-dispatch contract."
                ),
                key_concern=f"[netdev-private-framework-invariant] {netdev_cause}",
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
            val_str = ("" if val is None else str(val)).upper()
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


def _witness_indicates_null_guard_violation(
    func: "FunctionInfo", cex: "Counterexample"
) -> str | None:
    """Return a description if the counterexample shows a pointer
    variable as NULL where the function body has an explicit
    early-return guard ``if (!<ptr>) return ...;`` (or the equivalent
    ``if (<ptr> == NULL)`` / ``if (NULL == <ptr>)`` shapes). Otherwise
    None.

    Detection from the ch341.c sweep (2026-05-18, ch341_reset_resume):
    CBMC reports a NULL-deref via ``usb_get_serial_port_data`` even
    though the function explicitly has ``if (!priv) return 0;``
    immediately after the call. Symbolic execution should be unable
    to reach the deref under the witnessed state — this is a path-
    divergence artifact, not a bug.

    Conservative: only emits a reason when (a) the witness has at
    least one variable explicitly set to NULL, (b) that variable's
    NAME appears in a guard pattern near the top of the function
    body, and (c) the guard returns (rather than falling through).
    """
    if not cex or not getattr(cex, "variable_assignments", None):
        return None
    body = getattr(func, "body", None) or ""
    if not body:
        return None
    nulled_names: list[str] = []
    for var, val in (cex.variable_assignments or {}).items():
        # Witness values can be non-str (bool, int, dict, …) — CBMC's
        # ``variable_assignments`` is heterogeneous. Coerce defensively.
        val_str = "" if val is None else str(val)
        # Look for ``NULL`` or ``((...)NULL)`` patterns and exclude ``NOTNULL``.
        if "NULL" in val_str.upper() and "NOTNULL" not in val_str.upper():
            # Strip CBMC qualifier suffixes (``priv!0@1``, ``priv$$1``)
            # and leading ``_`` decorations. Take the base identifier.
            base = re.split(r"[!\$@\.\[]", var, 1)[0].lstrip("_")
            if base and base.isidentifier():
                nulled_names.append(base)
    if not nulled_names:
        return None
    # Inspect the first ~80 statements of the function body for guards.
    body_head = body[:4000]  # generous slice; guards usually appear early
    for name in nulled_names:
        # Match: ``if (!name) return ...;`` / ``if (name == NULL) return ...;``
        # / ``if (NULL == name) return ...;`` (with optional whitespace).
        patterns = [
            rf"\bif\s*\(\s*!\s*{re.escape(name)}\s*\)\s*\{{?\s*return\b",
            rf"\bif\s*\(\s*{re.escape(name)}\s*==\s*NULL\s*\)\s*\{{?\s*return\b",
            rf"\bif\s*\(\s*NULL\s*==\s*{re.escape(name)}\s*\)\s*\{{?\s*return\b",
            # And the same shapes spelled as ``== 0`` / ``!= 0``-less
            # variants we sometimes see in kernel code.
            rf"\bif\s*\(\s*{re.escape(name)}\s*==\s*0\s*\)\s*\{{?\s*return\b",
        ]
        for pat in patterns:
            if re.search(pat, body_head):
                return (
                    f"witness assigns NULL to '{name}' but the function has "
                    f"an explicit early-return guard 'if (!{name}) return …;' "
                    "near the top of the body"
                )
    return None


# Per-callback witness-pointer names we treat as "framework-set" for the
# USB-serial dispatch. ``tty->driver_data`` and the ``port`` argument are
# both populated by the core before the callback runs; ``priv`` is the
# usb_get_serial_port_data() result for which the framework guarantees a
# valid driver-allocated struct between probe/remove.
_USB_SERIAL_FRAMEWORK_POINTER_NAMES = {
    "tty", "port", "priv", "serial",
    # Common per-driver private struct names follow ``priv``/``port``,
    # not enumerated here; the body-scan picks up the witness var by name
    # only — see _looks_like_framework_null_witness.
}

# Field-name shortlist for ``struct usb_serial_driver`` callback slots.
# Sourced from drivers/usb/serial/usb-serial.c — these are the dispatch
# table entries whose wrappers all deref tty->driver_data / port before
# invoking the driver callback. Restricting to this list keeps the
# detector precise (won't fire on functions registered into OTHER kernel
# subsystems that happen to be named in a usb_serial_driver struct).
_USB_SERIAL_DRIVER_CALLBACKS = {
    "open", "close", "dtr_rts", "carrier_raised", "init_termios",
    "throttle", "unthrottle", "tiocmget", "tiocmset", "tiocmiwait",
    "get_icount", "set_termios", "break_ctl", "chars_in_buffer",
    "wait_until_sent", "write_room", "write", "read_int_callback",
    "read_bulk_callback", "write_bulk_callback", "process_read_urb",
    "prepare_write_buffer", "port_probe", "port_remove", "attach",
    "release", "calc_num_ports", "probe", "disconnect", "suspend",
    "resume", "reset_resume", "ioctl",
}

# Regex matches ``struct usb_serial_driver <name> = { ... };`` — captures
# the body so we can scan for the ``.<callback> = <func.name>`` line.
_USB_SERIAL_DRIVER_DEF_RE = re.compile(
    r"struct\s+usb_serial_driver\s+\w+\s*=\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}",
    re.DOTALL,
)


def _function_registered_as_usb_serial_callback(
    func_name: str, parsed_file: "ParsedCFile"
) -> str | None:
    """If *func_name* appears as a ``.<callback> = <func_name>`` slot in a
    ``struct usb_serial_driver`` definition in the parsed file, return
    the slot name (e.g. ``"tiocmset"``). Otherwise None.
    """
    if not parsed_file or not func_name:
        return None
    source = getattr(parsed_file, "preprocessed_source", None)
    if source is None:
        # Fall back to concatenated bodies — unlikely to catch top-level
        # dispatch tables, but cheap if preprocessed_source is unset.
        source = "\n".join((parsed_file.function_bodies or {}).values())
    if not source:
        return None
    for m in _USB_SERIAL_DRIVER_DEF_RE.finditer(source):
        block = m.group(1)
        # Look for ``.callback = func_name`` (allowing ws + optional trailing comma/whitespace).
        slot_re = re.compile(
            rf"\.(\w+)\s*=\s*(?:&\s*)?{re.escape(func_name)}\b"
        )
        for slot_m in slot_re.finditer(block):
            slot = slot_m.group(1)
            if slot in _USB_SERIAL_DRIVER_CALLBACKS:
                return slot
    return None


def _witness_assigns_null_to_framework_pointer(cex: "Counterexample") -> list[str]:
    """Return a list of witness variable basenames assigned NULL whose
    name matches a known framework-set pointer (tty, port, priv, …).
    """
    if not cex or not getattr(cex, "variable_assignments", None):
        return []
    matches: list[str] = []
    for var, val in cex.variable_assignments.items():
        val_str = ("" if val is None else str(val)).upper()
        if "NULL" not in val_str or "NOTNULL" in val_str:
            continue
        base = re.split(r"[!\$@\.\[]", var, 1)[0].lstrip("_")
        if not base or not base.isidentifier():
            continue
        # Direct framework-pointer name (``tty``, ``port``, ``priv``, ``serial``)
        # OR a field-access on ``tty`` (e.g. ``tty.driver_data``,
        # ``tty->driver_data`` rendered as ``tty.driver_data`` by CBMC).
        if base in _USB_SERIAL_FRAMEWORK_POINTER_NAMES:
            matches.append(var)
            continue
        # ``tty->driver_data`` appears as ``tty.driver_data`` or
        # ``*tty.driver_data`` in CBMC output. ``var`` may contain ``.``
        # after the basename strip; split the raw var to look for it.
        if "." in var or "->" in var:
            # Pull the head segment of the raw var
            head = re.split(r"[\.\->\[]", var, 1)[0].lstrip("_")
            if head in _USB_SERIAL_FRAMEWORK_POINTER_NAMES:
                matches.append(var)
    return matches


def _witness_indicates_usb_serial_framework_invariant(
    func: "FunctionInfo", cex: "Counterexample", parsed_file: "ParsedCFile"
) -> str | None:
    """Detect the pl2303-style false positive: a function registered as a
    USB-serial driver callback, with a witness that sets a
    framework-managed pointer (``tty``, ``port``, ``priv``, or
    ``tty->driver_data``) to NULL. The USB-serial core's wrapper
    derefs these before dispatching, so NULL is unreachable along any
    framework-constructed path.

    Conservative: requires BOTH (a) explicit registration as a
    USB-serial callback in a ``struct usb_serial_driver`` definition
    present in the parsed file, AND (b) at least one framework-pointer
    witness assignment to NULL.
    """
    callback_slot = _function_registered_as_usb_serial_callback(
        func.name, parsed_file
    )
    if not callback_slot:
        return None
    null_witnesses = _witness_assigns_null_to_framework_pointer(cex)
    if not null_witnesses:
        return None
    sample = ", ".join(sorted(set(null_witnesses))[:3])
    more = "" if len(null_witnesses) <= 3 else f" (+{len(null_witnesses)-3} more)"
    return (
        f"'{func.name}' is registered as the '.{callback_slot}' slot of a "
        f"struct usb_serial_driver dispatch table; witness assigns NULL to "
        f"framework-managed pointer(s) [{sample}{more}] that the core's "
        f"wrapper derefs before dispatching"
    )


# PHY-framework counterpart to the USB-serial detector. The Linux PHY
# subsystem's ``phy_ethtool_set_wol`` / ``phy_ethtool_get_wol`` /
# ``phy_start_aneg`` / ``phy_config_aneg`` wrappers all reach the driver
# callback only after the PHY has been attached via
# ``phy_attach_direct(dev, phydev, ...)``, which in the same ``if (dev) {
# ... }`` block sets both ``phydev->attached_dev = dev`` and
# ``dev->phydev = phydev``. The ethtool path uses ``dev->phydev`` to
# locate the phy, so any path that reaches a registered callback has
# ``attached_dev != NULL`` by construction. All in-tree callers of
# ``phy_attach_direct`` pass non-NULL ``dev``; the ``if (dev)`` guard
# exists for an explicitly-allowed but in-tree-unused configuration.
_PHY_FRAMEWORK_POINTER_NAMES = {
    "phydev", "attached_dev", "ndev", "dev",
}

# Field-name shortlist for ``struct phy_driver`` callback slots that
# the framework guarantees are reached only after attach. Sourced from
# include/linux/phy.h. The list deliberately excludes ``probe`` and
# ``remove`` because those run *during* attach and may legitimately
# see ``attached_dev == NULL``.
_PHY_DRIVER_CALLBACKS = {
    "soft_reset", "config_init", "config_aneg", "aneg_done",
    "read_status", "config_intr", "handle_interrupt", "did_interrupt",
    "ack_interrupt", "suspend", "resume",
    "get_wol", "set_wol",
    "link_change_notify", "read_mmd", "write_mmd",
    "get_tunable", "set_tunable", "get_features", "config_index",
    "get_strings", "get_sset_count", "get_stats", "get_phy_stats",
    "get_link_stats", "set_loopback", "get_loopback",
    "match_phy_device", "module_info", "module_eeprom",
    "cable_test_start", "cable_test_get_status", "led_brightness_set",
    "led_blink_set", "led_hw_is_supported", "led_hw_control_get",
    "led_hw_control_set", "led_polarity_set",
}

_PHY_DRIVER_DEF_RE = re.compile(
    r"struct\s+phy_driver\s+\w+(?:\s*\[\s*\w*\s*\])?\s*=\s*\{(.*?)\}\s*;",
    re.DOTALL,
)


def _function_registered_as_phy_driver_callback(
    func_name: str, parsed_file: "ParsedCFile"
) -> str | None:
    """If *func_name* appears as ``.<callback> = <func_name>`` in any
    ``struct phy_driver`` definition or array in the parsed file,
    return the slot name. Otherwise None.

    Many PHY drivers register an *array* of struct phy_driver entries
    (one per supported PHY ID), so the regex accepts both
    ``struct phy_driver X = {...}`` and
    ``struct phy_driver X[] = { {...}, {...} }`` shapes.
    """
    if not parsed_file or not func_name:
        return None
    source = getattr(parsed_file, "preprocessed_source", None)
    if source is None:
        source = "\n".join((parsed_file.function_bodies or {}).values())
    if not source:
        return None
    for m in _PHY_DRIVER_DEF_RE.finditer(source):
        block = m.group(1)
        slot_re = re.compile(
            rf"\.(\w+)\s*=\s*(?:&\s*)?{re.escape(func_name)}\b"
        )
        for slot_m in slot_re.finditer(block):
            slot = slot_m.group(1)
            if slot in _PHY_DRIVER_CALLBACKS:
                return slot
    return None


def _witness_assigns_null_to_phy_framework_pointer(
    cex: "Counterexample",
) -> list[str]:
    """Return a list of witness variable basenames assigned NULL whose
    name matches a known PHY-framework-set pointer
    (``phydev``, ``attached_dev``, ``ndev``, …) — including field-access
    forms like ``phydev.attached_dev`` and ``phydev->attached_dev``.
    """
    if not cex or not getattr(cex, "variable_assignments", None):
        return []
    matches: list[str] = []
    for var, val in cex.variable_assignments.items():
        val_str = ("" if val is None else str(val)).upper()
        if "NULL" not in val_str or "NOTNULL" in val_str:
            continue
        base = re.split(r"[!\$@\.\[]", var, 1)[0].lstrip("_")
        if not base or not base.isidentifier():
            continue
        if base in _PHY_FRAMEWORK_POINTER_NAMES:
            matches.append(var)
            continue
        # Field-access shape: ``phydev.attached_dev``,
        # ``phydev->attached_dev`` (CBMC may render as the former).
        if "." in var or "->" in var:
            head = re.split(r"[\.\->\[]", var, 1)[0].lstrip("_")
            tail = var[len(re.split(r"[\.\->\[]", var, 1)[0]):]
            if head in _PHY_FRAMEWORK_POINTER_NAMES:
                matches.append(var)
                continue
            # ``phydev.attached_dev`` — match attached_dev as a field.
            if any(f in tail for f in ("attached_dev", "dev_addr")):
                matches.append(var)
    return matches


def _witness_indicates_phy_framework_invariant(
    func: "FunctionInfo", cex: "Counterexample", parsed_file: "ParsedCFile"
) -> str | None:
    """Detect the dp83tc811-style false positive: a function registered
    as a ``struct phy_driver`` callback, with a witness that sets a
    framework-managed pointer (``phydev``, ``phydev->attached_dev``) to
    NULL. The PHY framework's ``phy_ethtool_*`` wrappers only reach the
    driver callback after ``phy_attach_direct(dev, phydev, ...)`` has
    set both ``phydev->attached_dev = dev`` and ``dev->phydev = phydev``
    in the same ``if (dev) { ... }`` block; the ethtool path uses
    ``dev->phydev`` to locate the phy, so ``attached_dev != NULL`` is a
    structural invariant of any framework-constructed path.

    Conservative: requires BOTH (a) explicit registration as a
    ``struct phy_driver`` callback in the parsed file (excluding
    ``probe``/``remove`` which run *during* attach), AND (b) at least
    one framework-pointer witness assignment to NULL.
    """
    callback_slot = _function_registered_as_phy_driver_callback(
        func.name, parsed_file
    )
    if not callback_slot:
        return None
    null_witnesses = _witness_assigns_null_to_phy_framework_pointer(cex)
    if not null_witnesses:
        return None
    sample = ", ".join(sorted(set(null_witnesses))[:3])
    more = "" if len(null_witnesses) <= 3 else f" (+{len(null_witnesses)-3} more)"
    return (
        f"'{func.name}' is registered as the '.{callback_slot}' slot of a "
        f"struct phy_driver dispatch table; witness assigns NULL to "
        f"framework-managed pointer(s) [{sample}{more}] that the PHY core's "
        f"attach lifecycle guarantees non-NULL before any callback "
        f"dispatch reachable from in-tree code"
    )


# Netdev-private framework-invariant: probe-time back-pointer fields the
# Linux PCI/netdev cores guarantee non-NULL before any registered callback
# (ndo_*, ethtool_ops.*, etc.) can be dispatched. ``alloc_etherdev`` /
# ``alloc_netdev`` allocate the private struct as part of the net_device
# layout and probe() conventionally sets the back-pointers below before
# returning success. Drivers that fail probe never register, so these
# are framework invariants of every dispatch-reachable path.
_NETDEV_BACKPOINTER_FIELDS = (
    "pci_dev", "netdev", "pdev", "mii_bus", "mmio_addr",
    # ``dev`` alone is broad; we match it only as a child field of an
    # identifier (``priv->dev``, ``tp->dev``), which is the standard
    # back-pointer convention. Bare ``dev`` parameters are handled by
    # the host-identifier check in the regex below.
    "dev",
)

# Net_device_ops / ethtool_ops registrations a driver file is expected
# to contain. Listing both keeps the gate firing on ethtool-only sub-
# files (e.g. r8125_rss.c which provides .set_rxnfc but no .ndo_*).
_NETDEV_REGISTRATION_RE = re.compile(
    r"\bstruct\s+(?:net_device_ops|ethtool_ops|pci_driver)\s+\w+(?:\s*\[\s*\w*\s*\])?\s*=\s*\{",
)

# Failing description shape from CBMC:
#   "dereference failure: pointer NULL in tp->dev"
#   "dereference failure: pointer NULL in tp->pci_dev->resource[...]..."
#   "dereference failure: pointer NULL in (&tp->pci_dev->resource[2])->end"
# We extract the *first* host->field pair in the description text and
# check whether ``field`` is in _NETDEV_BACKPOINTER_FIELDS.
_BACKPOINTER_FIELD_RE = re.compile(
    r"(?:[\(\&\*\s])([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*)"
)


def _file_registers_netdev_ops(parsed_file: "ParsedCFile") -> bool:
    """True if the parsed source contains at least one
    ``struct net_device_ops``/``ethtool_ops``/``pci_driver`` table
    definition — a strong signal the file is a PCI/netdev driver.
    """
    if not parsed_file:
        return False
    source = getattr(parsed_file, "preprocessed_source", None)
    if source is None:
        source = "\n".join((parsed_file.function_bodies or {}).values())
    if not source:
        return False
    return bool(_NETDEV_REGISTRATION_RE.search(source))


def _failing_deref_is_netdev_backpointer(
    description: str,
) -> tuple[str, str] | None:
    """Parse CBMC's failing-property description for the first
    ``<host>-><field>`` expression and return ``(host, field)`` iff
    *field* is one of the netdev back-pointer names. Otherwise None.

    The host identifier requirement filters out unrelated
    ``something->dev`` accesses where ``something`` is itself a
    framework type (e.g. ``netdev->dev`` is fine to leave to the LLM).
    """
    if not description:
        return None
    if "NULL" not in description.upper():
        return None
    for m in _BACKPOINTER_FIELD_RE.finditer(description):
        host, field = m.group(1), m.group(2)
        if field in _NETDEV_BACKPOINTER_FIELDS:
            # Skip the trivial ``netdev->dev`` / ``net_device->dev``
            # cases — those are *into* the framework struct, not a
            # back-pointer set by probe.
            if field == "dev" and host in ("netdev", "net_device", "ndev"):
                continue
            return host, field
    return None


def _witness_indicates_netdev_private_framework_invariant(
    func: "FunctionInfo", cex: "Counterexample", parsed_file: "ParsedCFile"
) -> str | None:
    """Detect the rtl8125-style false positive: a function in a PCI/
    netdev driver file whose failing dereference is on a probe-set
    back-pointer of the driver's private struct
    (``priv->pci_dev``/``priv->dev``/…). The kernel's
    ``alloc_etherdev`` + driver ``probe`` lifecycle assigns these
    fields before ``register_netdev`` makes the device visible to any
    registered callback, so the NULL state is unreachable along any
    framework-constructed dispatch path.

    Conservative: requires BOTH (a) at least one
    ``struct net_device_ops``/``ethtool_ops``/``pci_driver`` table in
    the parsed file, AND (b) the failing dereference description
    matches a ``<host>-><backpointer-field>`` expression. Excludes
    probe-shaped function names where the back-pointers are
    legitimately transient.
    """
    if not _file_registers_netdev_ops(parsed_file):
        return None
    desc = getattr(cex, "description", "") or ""
    hit = _failing_deref_is_netdev_backpointer(desc)
    if not hit:
        return None
    host, field = hit
    # Exclude probe-shaped functions where these back-pointers may
    # legitimately be NULL transiently. The kernel-style names below
    # cover the common cases; we err on the side of *not* filtering
    # if we can't be sure.
    name_lc = (func.name or "").lower()
    probe_markers = ("probe", "_init_one", "_init_module", "_create", "_alloc")
    if any(tag in name_lc for tag in probe_markers):
        return None
    return (
        f"failing dereference '{host}->{field}' is a probe-time back-pointer "
        "of the driver-private struct; the file registers a "
        "net_device_ops/ethtool_ops/pci_driver dispatch table, so "
        "callbacks reachable in-tree run only after probe() has set the "
        "field"
    )


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
