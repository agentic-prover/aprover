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
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

from bmc_agent.logger import get_logger
from bmc_agent.prompts import (
    ADJACENT_BUG_PROMPT,
    REALISM_CHECK_PROMPT,
    SPEC_SYSTEM_PROMPT,
    THREAT_MODEL_CONTEXT,
)

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
    adjacent_bugs: list = field(default_factory=list)  # other bugs spotted nearby

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reasoning": self.reasoning,
            "key_concern": self.key_concern,
            "llm_confidence": self.llm_confidence,
            "adjacent_bugs": list(self.adjacent_bugs or []),
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
        cbmc_harness_path: str = "",
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

        # Intentional-narrow-cast detector (from r8125_fiber round-2 FP,
        # 2026-05-18): --conversion-check flags ``(u8)read_u32_register()``
        # as "arithmetic overflow on unsigned to unsigned type conversion"
        # even though the cast is explicit in source. Driver register-IO
        # code routinely truncates a u32 return to extract a low byte;
        # this is defined C behavior and intentional. The realism stage
        # rejects the CEX without an LLM call when the failing description
        # localizes the conversion to a known narrow-integer target type.
        truncation_cause = _witness_indicates_intentional_truncation(counterexample)
        if truncation_cause:
            logger.info(
                "Realism check for '%s': UNREALISTIC by intentional "
                "narrow-integer cast — %s", func.name, truncation_cause,
            )
            return RealismCheckResult(
                verdict=RealismVerdict.UNREALISTIC,
                reasoning=(
                    f"Intentional narrow-integer truncation: {truncation_cause}. "
                    "C explicitly defines the value-narrowing semantics of "
                    "``(uN)expr`` casts; the programmer used the cast to take "
                    "the low N bits. --conversion-check flags every such cast "
                    "where the input may exceed the target's range — that is "
                    "almost always the entire point of the cast. Not a bug."
                ),
                key_concern=f"[intentional-truncation] {truncation_cause}",
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
            cbmc_harness_path=cbmc_harness_path,
        )

        try:
            # Use extended thinking when available to improve reasoning quality.
            use_thinking = getattr(self.config, "enable_realism_thinking", False)
            # max_tokens=2048 caused realism responses to truncate mid-JSON for
            # complex findings (observed in libxml2 xmlAddEntity); the parser
            # then fell back to prose-keyword recovery and picked the wrong
            # verdict. Bump to 4096; with thinking enabled the SDK auto-expands
            # to thinking_budget + 1024, so we override that calculation here.
            # Phase 4b: prepend learned skepticism hints to the system
            # prompt when the autonomous loop has accumulated them from
            # prior rounds. Empty string by default; never overrides
            # the static prompt's decision rules, only adds context.
            extra = (getattr(self.config, "realism_extra_skepticism", "") or "").strip()
            sys_prompt = (
                f"{extra}\n\n---\n\n{SPEC_SYSTEM_PROMPT}" if extra else SPEC_SYSTEM_PROMPT
            )

            # Adjacent-bug hint from a previous round, if any. The hint
            # text is the LLM's prior attacker_scenario describing a
            # suspected defect in THIS function. Prepend it to the
            # primary prompt so this round's verdict can verify or
            # refute the hypothesis directly against CBMC's CEx.
            hints = getattr(self.config, "function_hints", {}) or {}
            hint = (hints.get(func.name) or "").strip()
            user_prompt = prompt
            if hint:
                user_prompt = (
                    "PRIOR-ROUND HYPOTHESIS FROM ADJACENT-BUG DISCOVERY\n"
                    "===================================================\n"
                    f"{hint}\n\n"
                    "This function is being re-verified because a previous\n"
                    "round flagged it as potentially exploitable. CBMC has\n"
                    "now produced a counterexample (see below). Treat the\n"
                    "hypothesis above as ONE possibility — your job is still\n"
                    "to decide if CBMC's counterexample is realistic. If the\n"
                    "CEx matches the hypothesis, that's strong evidence for\n"
                    "REALISTIC. If CBMC's CEx is a different (artifact) path\n"
                    "but the hypothesis itself is exploitable, still vote\n"
                    "REALISTIC and explain the gap in your reasoning.\n\n"
                    "===================================================\n\n"
                    + prompt
                )

            raw = self.llm.complete(
                sys_prompt,
                user_prompt,
                max_tokens=4096 + (4000 if use_thinking else 0),
                thinking=use_thinking,
                thinking_budget=4000,
                role="realism",
            )
            pass1 = _parse_result(raw, func.name)

            # PASS 2 (disabled by default after the prompt simplification):
            # the new Pass-1 prompt already gives the LLM full context plus
            # a clear attacker-exploitability definition, so a second
            # rule-based pass adds bias more than recall. Set
            # enable_realism_pass2=True only for forensic debugging.
            if (
                pass1.verdict == RealismVerdict.UNREALISTIC
                and getattr(self.config, "enable_realism_pass2", False)
            ):
                pass2_prompt = _build_pass2_prompt(
                    func=func, counterexample=counterexample,
                    parsed_file=parsed_file, pass1_result=pass1,
                )
                try:
                    raw2 = self.llm.complete(
                        sys_prompt, pass2_prompt,
                        max_tokens=4096 + (4000 if use_thinking else 0),
                        thinking=use_thinking,
                        thinking_budget=4000,
                        role="realism",
                    )
                    pass2 = _parse_result(raw2, func.name)
                    if pass2.verdict != RealismVerdict.UNREALISTIC:
                        logger.info(
                            "Realism Pass 2 OVERRIDE for '%s': UNREALISTIC → %s",
                            func.name, pass2.verdict.value,
                        )
                        pass2.reasoning = (
                            "[pass2-override] " + (pass2.reasoning or "") +
                            "\n\nPass 1 reasoning: " + (pass1.reasoning or "")[:500]
                        )
                        return pass2
                    logger.info("Realism Pass 2 confirms UNREALISTIC for '%s'", func.name)
                except LLMError as exc2:
                    logger.warning(
                        "Realism Pass 2 LLM failed for '%s': %s — keeping Pass 1",
                        func.name, exc2,
                    )

            # ADJACENT-BUG DISCOVERY (independent second LLM call).
            # Fires ONLY when the primary verdict rejected the CBMC
            # counterexample (UNREALISTIC). The rationale: when the bug
            # is already confirmed REALISTIC we already have a finding
            # to act on — there's no need to spend a second LLM call
            # hunting for adjacent ones. When the CEx is rejected,
            # though, the function and its surroundings may still
            # contain a different exploitable defect that CBMC's witness
            # didn't capture; that's exactly the case worth probing.
            # UNCERTAIN also skips the second call (cheap default).
            if (
                getattr(self.config, "enable_adjacent_bug_discovery", True)
                and pass1.verdict == RealismVerdict.UNREALISTIC
            ):
                try:
                    adj_prompt = self._build_adjacent_bug_prompt(
                        func=func,
                        counterexample=counterexample,
                        validation_result=validation_result,
                        parsed_file=parsed_file,
                        all_funcs=all_funcs,
                    )
                    raw_adj = self.llm.complete(
                        sys_prompt, adj_prompt,
                        max_tokens=4096,
                        thinking=False,
                        role="realism",
                    )
                    adj_list = _parse_adjacent_bugs(raw_adj, func.name)
                    if adj_list:
                        logger.info(
                            "Adjacent-bug pass found %d candidate(s) for '%s'",
                            len(adj_list), func.name,
                        )
                        pass1.adjacent_bugs = adj_list
                except LLMError as exc_adj:
                    logger.warning(
                        "Adjacent-bug LLM call failed for '%s': %s — skipping",
                        func.name, exc_adj,
                    )

            # (A) ADAPTIVE HARNESS RETRY — track repeat rejections per function.
            # When the LLM rejects this function's CBMC findings for the same
            # reason ≥2 times in the current sweep, set a flag the harness
            # generator reads next round so it widens cast-chain init for
            # that function (more typed backings, deeper struct expansion).
            if pass1.verdict == RealismVerdict.UNREALISTIC:
                cls_text = (pass1.key_concern or pass1.reasoning or "")[:160].strip().lower()
                if cls_text:
                    history = getattr(self.config, "rejection_history", None)
                    if history is None:
                        history = {}
                        self.config.rejection_history = history
                    fn_hist = history.setdefault(func.name, {})
                    fn_hist[cls_text] = fn_hist.get(cls_text, 0) + 1
                    if fn_hist[cls_text] >= 2:
                        widen = getattr(self.config, "harness_widen_targets", None)
                        if widen is None:
                            widen = set()
                            self.config.harness_widen_targets = widen
                        if func.name not in widen:
                            widen.add(func.name)
                            logger.info(
                                "(A) Repeat-rejection on '%s' — flagging for "
                                "wider cast-chain init in next harness pass",
                                func.name,
                            )

            return pass1
        except LLMError as exc:
            logger.warning("Realism check LLM call failed for '%s': %s", func.name, exc)
            return RealismCheckResult(
                verdict=RealismVerdict.UNCERTAIN,
                reasoning=f"LLM call failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Tool-use augmentation (commit 3 of the tool-use trilogy)
    # ------------------------------------------------------------------

    def check_with_tools_if_enabled(
        self,
        func: "FunctionInfo",
        counterexample: "Counterexample",
        validation_result: "ValidationResult",
        parsed_file: "ParsedCFile",
        all_funcs: "dict[str, FunctionInfo]",
        spec: "Spec",
        all_specs: "Optional[dict[str, Spec]]" = None,
        cbmc_harness_path: str = "",
    ) -> RealismCheckResult:
        """Wrap :meth:`check` with an optional tool-use augmentation pass.

        Always runs the base check first. If ``config.enable_realism_tools``
        is on AND the base verdict is UNCERTAIN/UNREALISTIC, fires a
        second LLM call with tool access so the LLM can verify call
        chains, look up callee bodies, and check callee POSTs against
        the witness state.

        REALISTIC verdicts are kept as-is — augmenting a "realistic"
        finding with tools would only weaken it, and we don't want
        the tool-use branch to silently demote real-bug candidates.

        Returns the augmented result on success; the base result if
        the augmentation fails / declines / times out.
        """
        base = self.check(
            func=func, counterexample=counterexample,
            validation_result=validation_result, parsed_file=parsed_file,
            all_funcs=all_funcs, spec=spec,
            cbmc_harness_path=cbmc_harness_path,
        )
        if not getattr(self.config, "enable_realism_tools", False):
            return base
        if base.verdict == RealismVerdict.REALISTIC:
            return base   # don't second-guess REALISTIC findings
        try:
            augmented = self._augment_with_tools(
                base_result=base, func=func, counterexample=counterexample,
                spec=spec, parsed_file=parsed_file,
                all_specs=all_specs or {},
            )
        except Exception as exc:
            logger.warning(
                "Realism tool-use augmentation failed for '%s' "
                "(%r) — keeping base verdict", func.name, exc,
            )
            return base
        return augmented if augmented is not None else base

    def _augment_with_tools(
        self,
        *,
        base_result: RealismCheckResult,
        func: "FunctionInfo",
        counterexample: "Counterexample",
        spec: "Spec",
        parsed_file: "ParsedCFile",
        all_specs: "dict[str, Spec]",
    ) -> Optional[RealismCheckResult]:
        """Run the tool-use realism call. Returns the parsed augmented
        result, or None if the call/parse failed."""
        from bmc_agent.llm import LLMError
        from bmc_agent.realism_tools import (
            RealismToolContext, build_realism_tools,
            TOOL_USE_PROMPT_ADDENDUM,
        )
        ctx = RealismToolContext(
            parsed=parsed_file,
            all_specs=dict(all_specs),
        )
        tools, handlers = build_realism_tools(ctx)

        # Reuse the base prompt + append the tool-use addendum + the
        # base verdict so the LLM can refine it.
        try:
            base_prompt = self._build_prompt(
                func=func,
                counterexample=counterexample,
                validation_result=None,  # not used in pass-2 reconsideration
                parsed_file=parsed_file,
                all_funcs={},   # tools fetch this on demand
                spec=spec,
                cbmc_harness_path="",
            )
        except Exception:
            base_prompt = (
                f"Reconsider realism for {func.name}'s CEx "
                f"{counterexample.failing_property}."
            )

        reconsider = (
            f"{base_prompt}\n\n"
            f"--- BASE VERDICT (under reconsideration) ---\n"
            f"verdict: {base_result.verdict.value}\n"
            f"reasoning: {(base_result.reasoning or '')[:1000]}\n"
            f"key_concern: {(base_result.key_concern or '')[:300]}\n"
            f"{TOOL_USE_PROMPT_ADDENDUM}"
        )

        try:
            tu_result = self.llm.complete_with_tools(
                system_prompt=SPEC_SYSTEM_PROMPT,
                user_prompt=reconsider,
                tools=tools,
                tool_handlers=handlers,
                max_iterations=6,
                max_tool_calls=3,
                max_tokens_per_turn=4096,
                role="realism",
            )
        except LLMError as exc:
            logger.warning("Realism tool-use call failed for '%s': %s",
                           func.name, exc)
            return None
        if tu_result.error:
            logger.info(
                "Realism tool-use terminated for '%s': %s "
                "(iterations=%d, tool_calls=%d)",
                func.name, tu_result.error,
                tu_result.iterations, tu_result.tool_calls_made,
            )
            return None

        parsed_result = _parse_result(tu_result.text, func.name)
        if parsed_result is None:
            return None
        # If augmentation flipped to REALISTIC, log the divergence —
        # base UNREALISTIC → augmented REALISTIC suggests the base
        # call missed evidence the tools surfaced.
        if (
            parsed_result.verdict == RealismVerdict.REALISTIC
            and base_result.verdict != RealismVerdict.REALISTIC
        ):
            logger.warning(
                "Realism augmentation for '%s': base=%s → tool-use=REALISTIC "
                "(tool_calls=%d). The tool-use evidence promoted the verdict; "
                "this is the rare case where the base check was over-confident.",
                func.name, base_result.verdict.value,
                tu_result.tool_calls_made,
            )
        else:
            logger.info(
                "Realism augmentation for '%s': %s → %s (tool_calls=%d)",
                func.name, base_result.verdict.value,
                parsed_result.verdict.value, tu_result.tool_calls_made,
            )
        return parsed_result

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
        cbmc_harness_path: str = "",
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
        harness_code = _format_harness_code(validation_result, cbmc_harness_path)

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

        # Active stub contracts — list every callee in func.callees that
        # the harness generated a contracted stub for, with a plain-English
        # summary of the contract. This is the SAME library-level knowledge
        # a human triager uses to spot stub-callee-disconnect FPs. The
        # realism prompt's new decision rule (UNREALISTIC #5) tells the
        # LLM to compare the counterexample witness against these contracts
        # and reject violations as unreachable.
        try:
            from bmc_agent.universal_stub_contracts import format_active_contracts
            active_stub_contracts = format_active_contracts(getattr(func, "callees", set())) or "(no registered contracts apply to this function's callees)"
        except Exception:
            active_stub_contracts = "(stub-contract list unavailable)"

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
            active_stub_contracts=active_stub_contracts,
        )

    def _build_adjacent_bug_prompt(
        self,
        func: "FunctionInfo",
        counterexample: "Counterexample",
        validation_result: "ValidationResult",
        parsed_file: "ParsedCFile",
        all_funcs: "dict[str, FunctionInfo]",
    ) -> str:
        """Build the prompt for the second (independent) adjacent-bug LLM
        call. Re-uses the same context blob the primary realism prompt
        does, but asks a different question and returns a different JSON
        shape. Kept separate so the primary verdict's reasoning budget
        isn't diluted and the LLM isn't primed to suspect the primary
        CBMC finding."""
        sig = _format_signature(func)
        body = func.body or "(body not available)"
        violated = _format_failing_property_with_location(counterexample)
        cex_state = _format_cex_state(counterexample)
        call_chain_str = " → ".join(validation_result.caller_path) if validation_result.caller_path else func.name
        caller_context = _format_caller_context(
            validation_result.caller_path, all_funcs, parsed_file
        )
        call_site_analysis = _format_call_site_analysis(
            func.name, validation_result.caller_path, all_funcs, parsed_file
        )
        failure_line = None
        loc = getattr(counterexample, "failure_location", None) or {}
        if isinstance(loc, dict):
            try:
                failure_line = int(loc.get("line", "")) if loc.get("line") else None
            except (TypeError, ValueError):
                failure_line = None
        source_file_context = _format_source_file_context(parsed_file, mark_line=failure_line)
        try:
            from bmc_agent.universal_stub_contracts import format_active_contracts
            active_stub_contracts = format_active_contracts(getattr(func, "callees", set())) or "(no registered contracts apply to this function's callees)"
        except Exception:
            active_stub_contracts = "(stub-contract list unavailable)"

        return ADJACENT_BUG_PROMPT.format(
            function_name=func.name,
            function_signature=sig,
            function_body=body[:8000],
            violated_property=violated,
            counterexample_state=cex_state,
            call_chain=call_chain_str,
            caller_context=caller_context,
            call_site_analysis=call_site_analysis,
            active_stub_contracts=active_stub_contracts,
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


def _build_pass2_prompt(
    func: "FunctionInfo",
    counterexample: "Counterexample",
    parsed_file: "ParsedCFile",
    pass1_result: "RealismCheckResult",
) -> str:
    """Pass 2 realism prompt: forces exhaustive caller-enumeration.

    Pass 1 rejected this finding as UNREALISTIC. The dominant Pass-1
    failure mode is the existential-to-universal leap: the LLM finds
    ONE caller path that enforces the relevant invariant and concludes
    ALL paths do, without enumerating. Pass 2 forces explicit
    enumeration of every caller and every path before accepting
    UNREALISTIC.

    Returns the user-side prompt (system prompt is unchanged).
    """
    pass1_reasoning = (pass1_result.reasoning or "")[:2000]
    pass1_key_concern = (pass1_result.key_concern or "")[:600]
    fn_name = func.name
    # Try to enumerate callers from the parsed file's call graph
    callers_in_file: list[str] = []
    cg = getattr(parsed_file, "call_graph", {}) or {}
    for caller_name, callees in cg.items():
        if fn_name in callees:
            callers_in_file.append(caller_name)
    callers_str = (
        ", ".join(sorted(set(callers_in_file))) if callers_in_file
        else "(no direct callers found in this file — may be a vtable callback or external entry)"
    )
    # The same source file context is appended later; here we focus
    # on the directive.
    src = getattr(parsed_file, "preprocessed_source", None)
    if not src:
        try:
            with open(getattr(parsed_file, "path", ""), "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except Exception:
            src = "(source unavailable)"
    src_excerpt = src[:120000] if src else "(source unavailable)"

    return f"""\
PASS 2: EXHAUSTIVE CALLER-ENUMERATION CHECK

Pass 1 of realism analysis rejected this finding as UNREALISTIC, with
reasoning:

  Pass-1 verdict:  {pass1_result.verdict.value}
  Pass-1 key concern:  {pass1_key_concern}
  Pass-1 reasoning excerpt:  {pass1_reasoning}

A dominant Pass-1 failure mode is the EXISTENTIAL-TO-UNIVERSAL LEAP:
Pass 1 finds ONE call site or one allocation path that enforces the
relevant invariant, and concludes ALL paths do — without enumerating
the others. Real seed bugs almost always exist because some specific
path violates an invariant Pass 1 assumed was universal.

YOUR TASK: rigorously verify Pass 1's UNREALISTIC verdict by EXPLICIT
ENUMERATION. If you find ANY caller path that can produce the witness
state, the verdict should be REALISTIC (or UNCERTAIN if uncertain).

FUNCTION UNDER TEST: {fn_name}

VIOLATED PROPERTY: {counterexample.failing_property}

COUNTEREXAMPLE STATE (key witness assignments):
{chr(10).join(f"  {k} = {v}" for k, v in list((counterexample.variable_assignments or {}).items())[:30])}

DIRECT CALLERS in this file (from the parsed call graph):
  {callers_str}

FULL SOURCE FILE CONTEXT (for caller-path tracing):
```c
{src_excerpt}
```

ENUMERATION PROCEDURE (mandatory):

Step 1. Find EVERY caller of `{fn_name}` in the source file above.
        (Don't trust the call-graph extract above — it may be
        incomplete. Search the source for all call sites.)

Step 2. For EACH caller, identify the call sites within the caller's
        body where `{fn_name}` is invoked, and the state of the
        program at those call sites.

Step 3. For each call site, ask: "Could the witness state
        (specifically the values in COUNTEREXAMPLE STATE above) be
        produced by THIS caller's execution before THIS call?" Pay
        special attention to:
        - SKIP / CANCEL / SHORT-CIRCUIT paths in the caller
        - ERROR-RECOVERY paths where the caller didn't run the
          "normal" initialization
        - REPEATED-CALL / REINIT patterns where the function was
          called once already
        - Function-pointer / callback paths from outside this file

Step 4. ENUMERATE PATHOLOGICAL-BUT-LEGAL PUBLIC-API USAGE SEQUENCES.
        Many real bugs arise NOT from a single in-file caller path
        but from how the public API can be (mis)used by external
        code. For functions reachable via the public API (directly
        OR through vtable dispatch / framework callbacks), ask:

        - DOUBLE-INIT / DOUBLE-REGISTER: what if the public-API
          entry that ultimately invokes `{fn_name}` is called more
          than once? Does state from the first call persist into
          the second? (Generic pattern: a "register" or "init"
          public API documented as "call once" doesn't enforce
          single-call. Second invocation can dispatch a cleanup
          callback on stale or already-freed state, producing a
          crash. Many real bug fixes add re-entry safety.)
        - CALL-AFTER-FREE: what if the public-API entry is called
          AFTER another that freed resources? Is `{fn_name}` invoked
          with state that's already been cleaned up?
        - INTERLEAVED CALLS: what if the public API is called in
          an order the docs don't explicitly forbid but the
          implementation doesn't handle (e.g. read_data before
          read_header, free before complete)?
        - ZERO-INIT: what if the public-API entry's normal init
          path was skipped (early-return on malformed input,
          allocation failure, etc.) and `{fn_name}` is invoked
          on partially-initialized state?
        - ATTACKER-CONTROLLED-INPUT: if `{fn_name}` processes any
          external input (file bytes, network data, user-supplied
          buffer), what minimal malformed input produces the
          witness state? Most parsers / decoders / handlers
          process untrusted input, and the witness usually maps
          to "what input triggers this?".

        These usage patterns are LEGAL for the user to attempt
        (the public API doesn't reject them with hard assertions)
        but may produce the witness state. If ANY pathological
        sequence produces the witness state, vote REALISTIC.
        Do NOT assume "the framework guarantees X is only called
        once" — frameworks are robust to whatever public-API calls
        users actually make, and "only called once" is rarely an
        enforced invariant.

Step 5. If ANY caller path or pathological public-API sequence can
        produce the witness state → vote REALISTIC. If you can't
        decide for some paths → vote UNCERTAIN. ONLY vote
        UNREALISTIC if you've enumerated every caller path AND
        every pathological-but-legal usage sequence and confirmed
        NONE produce the state.

Step 6. Write your enumeration explicitly in the reasoning. For
        each caller you considered, state "Caller X: path Y,
        witness producible?  Yes/No/Uncertain — because Z."
        For pathological sequences, state "Pattern P: sequence
        is/isn't producible because Q."

Pass 1's reasoning is presumed WRONG if it lacks this explicit
enumeration. Only confirm UNREALISTIC if your enumeration agrees
with Pass 1's conclusion.

Respond with ONLY valid JSON (same schema as Pass 1):
{{
  "verdict": "REALISTIC" | "UNREALISTIC" | "UNCERTAIN",
  "reasoning": "<step-by-step enumeration: list each caller and the path-state analysis>",
  "source_line_guard": "<see REQ-1 in Pass-1 prompt — same rule>",
  "public_api_call_chain": "<see REQ-2 in Pass-1 prompt — same rule>",
  "key_concern": "<what would happen to a real user if this is a real bug — or why all paths are safe if not>",
  "confidence": "high" | "medium" | "low"
}}
"""


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


# Narrow-integer target types where an explicit cast in C is conventionally
# used to *take the low bits*, not to assert the value fits. CBMC's
# --conversion-check fires on every cast where the source value can be
# wider than the target; for register reads and bit-extraction idioms,
# that's the entire purpose of the cast and not a bug.
_NARROW_INT_TARGET_TYPES = frozenset({
    "u8", "u16", "u32",
    "__u8", "__u16", "__u32",
    "uint8_t", "uint16_t", "uint32_t",
    "int8_t", "int16_t", "int32_t",
    "char", "short", "signed char", "unsigned char",
    "u_int8_t", "u_int16_t", "u_int32_t",
    "uchar", "byte",
})

# Match the cast target in CBMC's overflow description, which has the
# canonical form:
#   "arithmetic overflow on <kind> to <kind> type conversion in (TARGET)expr"
# We extract TARGET and check if it's in the narrow-int allowlist.
_CONVERSION_DESC_RE = re.compile(
    r"arithmetic\s+overflow\s+on\s+\w+\s+to\s+\w+\s+type\s+conversion\s+in\s+\(([\w\s_]+)\)",
    re.IGNORECASE,
)


def _witness_indicates_intentional_truncation(
    cex: "Counterexample",
) -> str | None:
    """Detect the r8125_fiber-style false positive: CBMC's
    --conversion-check flags an explicit narrow-int cast in source
    (``(u8)read_u32(...)``) as overflow. The cast is intentional C
    truncation, not a bug. Returns a description string when the
    detector fires; otherwise None.

    Conservative: requires BOTH (a) the failing description matches the
    "overflow on ... type conversion in (T)expr" template, AND (b) the
    target type T is in the narrow-int allowlist. We deliberately do
    NOT fire on user-defined narrowing target types — those are
    typically pointer-sized or struct types where unintended narrowing
    is the actual bug.
    """
    desc = getattr(cex, "description", "") or ""
    if "type conversion" not in desc.lower():
        return None
    m = _CONVERSION_DESC_RE.search(desc)
    if not m:
        return None
    target_type = m.group(1).strip()
    # Normalize whitespace within compound type names (e.g. "signed char").
    target_type = re.sub(r"\s+", " ", target_type)
    if target_type not in _NARROW_INT_TARGET_TYPES:
        return None
    return (
        f"explicit cast to narrow integer '{target_type}' "
        f"(description: '{desc[:80]}...'); --conversion-check fires "
        "because the source value may exceed the target's range, but "
        "C semantics define the truncation and the programmer chose it"
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
    """Format the dynamic-validation outcome for the realism prompt with
    EXPLICIT framing so the LLM can't hallucinate a different result.
    Each line is unambiguous and tells the LLM how to weight the signal.
    """
    dyn = getattr(vr, "dynamic_result", None)
    if dyn is None:
        return (
            "NOT RUN. The GCC+ASAN dynamic harness did not execute for this "
            "finding. Reason on static evidence only; do NOT assume any "
            "runtime crash occurred."
        )
    outcome = dyn.outcome.value if hasattr(dyn.outcome, "value") else str(dyn.outcome)
    sig = dyn.signal_name or "none"
    reasoning = (dyn.reasoning or "")[:200]
    if outcome == "confirmed":
        return (
            f"CONFIRMED (signal={sig}). The dynamic harness executed and "
            f"CRASHED at runtime with the witness values. This is STRONG "
            f"evidence the bug is real. Reasoning: {reasoning}"
        )
    if outcome == "not_triggered":
        return (
            f"NOT_TRIGGERED (no crash; signal={sig}). The dynamic harness "
            f"executed to completion with the CBMC witness values and did "
            f"NOT crash. The specific witness state did not reproduce a "
            f"fault on real libc. This is evidence against the CBMC witness "
            f"being a real bug — though a different attacker input may still "
            f"trigger the same bug class. Do NOT claim the harness "
            f"'confirmed' or 'aborted' or 'SIGABRT'd' — it ran clean. "
            f"Reasoning: {reasoning}"
        )
    if outcome == "inconclusive":
        return (
            f"INCONCLUSIVE (compile or run failed; signal={sig}). The "
            f"dynamic harness could not be executed (linker error, timeout, "
            f"missing symbol). No runtime evidence either way. "
            f"Reasoning: {reasoning}"
        )
    if outcome == "skipped":
        return (
            f"SKIPPED. Dynamic validation was disabled or not applicable. "
            f"No runtime evidence. Reasoning: {reasoning}"
        )
    return f"{outcome.upper()} (signal={sig}). {reasoning}"


def _format_harness_code(vr: "ValidationResult", cbmc_harness_path: str = "") -> str:
    """Return the harness source for the realism prompt.

    Preference order:
      1. CBMC harness file on disk (this is the harness whose state
         produced the counterexample — the only one whose initial-state
         setup can reveal harness-artifact FPs like uninitialized
         pointers, freed-but-not-nulled fields, non-public-API state).
      2. Dynamic harness source (smaller GCC+ASAN runtime check harness).
      3. LLM-generated system-entry reproducer.

    Cap at ~8000 chars so a typical 200-line CBMC harness fits whole.
    """
    if cbmc_harness_path:
        try:
            return open(cbmc_harness_path, "r").read()[:8000]
        except OSError:
            pass
    reproducer = vr.system_entry_input
    dyn = getattr(vr, "dynamic_result", None)
    harness_src = getattr(dyn, "harness_source", None) if dyn else None
    if harness_src:
        return harness_src[:8000]
    if reproducer:
        return reproducer[:8000]
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


def _parse_adjacent_bugs(raw: str, func_name: str) -> list[dict]:
    """Parse the second LLM call's response, which is `{"adjacent_bugs": [...]}`.
    Returns the normalized list (possibly empty). Tolerant of markdown fencing
    and prose-wrapped JSON; never raises."""
    text = (raw or "").strip()
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
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        embedded = _extract_first_json_object(text)
        if embedded is not None:
            try:
                data = json.loads(embedded)
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return []
    raw_list = data.get("adjacent_bugs") or []
    out: list[dict] = []
    if isinstance(raw_list, list):
        for entry in raw_list:
            if isinstance(entry, dict) and entry.get("attacker_scenario"):
                out.append({
                    "location": str(entry.get("location", "")).strip(),
                    "bug_type": str(entry.get("bug_type", "")).strip(),
                    "attacker_scenario": str(entry.get("attacker_scenario", "")).strip(),
                    "confidence": str(entry.get("confidence", "")).strip(),
                })
    return out


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
                # Set llm_confidence="medium" so the bug_reporter
                # honours the downgrade. The default (empty string)
                # would gate out the downgrade in the
                # ``llm_confidence in ("high", "medium")`` check,
                # silently preserving the finding as a confirmed bug
                # even though the LLM clearly returned UNREALISTIC.
                # Observed on cpio.c 2026-05-23 re-sweep: 5 UNREALISTIC
                # verdicts parsed from prose, ALL preserved as
                # confirmed bugs because of this gating issue.
                return RealismCheckResult(
                    verdict=recovered,
                    reasoning=raw.strip()[:2000],
                    llm_confidence="medium",
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
    # Accept the new attacker-focused field `exploit_scenario` or the
    # legacy `key_concern`; treat them as the same downstream concept.
    key_concern = (
        str(data.get("exploit_scenario", "")).strip()
        or str(data.get("key_concern", "")).strip()
    )
    llm_confidence = str(data.get("confidence", "")).strip()

    # Legacy evidence fields kept only so the auto-downgrade code (now a
    # no-op) doesn't NameError on its references.
    source_line_guard = ""
    public_api_call_chain = ""

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
    # Auto-downgrades disabled. The previous logic rejected REALISTIC
    # verdicts when the LLM's reasoning contained phrases like "stub
    # returns" or when the REQ-1/REQ-2 evidence fields were empty.
    # With the simplified attacker-exploitability prompt those fields
    # no longer exist, and the LLM reasons freely instead of completing
    # rule-templated answers — so any phrase-matching downgrade is
    # adding bias, not precision. Trust the LLM verdict.

    logger.info(
        "Realism check for '%s': verdict=%s confidence=%s",
        func_name, verdict.value, llm_confidence,
    )
    if verdict != RealismVerdict.REALISTIC and key_concern:
        logger.info("  Key concern: %s", key_concern[:150])

    # adjacent_bugs is now populated by the separate ADJACENT_BUG_PROMPT
    # call (see RealismChecker.check). The primary realism JSON shape no
    # longer carries adjacent_bugs, so nothing to parse here — the caller
    # assigns pass1.adjacent_bugs after the second LLM call returns.
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
