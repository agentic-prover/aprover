"""Caller-grounded, channel-guarded reachability for confirmed_dynamic findings.

A confirmed_dynamic runtime crash earns immunity from realism downgrade because
realism has false negatives on real bugs. But the unit-level harness also crashes
internal helpers on nondet argument values their real callers never pass (the
fb_draw_char / wsod / kapi false-confirmation class). This module decides whether
such immunity should be *suspended* — WITHOUT hiding real bugs — using two layers:

  1. CHANNEL GUARD. Classify where the faulting value originates:
       - ``argument``  : it comes directly from a scalar parameter the function
                         receives (e.g. fb_draw_char's x). Call sites reveal whether
                         real callers constrain it, so grounding is valid.
       - ``internal``  : it is READ from memory inside the body — a pointer's
                         contents, a struct field behind a pointer, a global, or an
                         MMIO/device read (vfs_open_handle's temp->data,
                         ip_handle's ip->total_len). Call sites are BLIND to this
                         channel, so grounding cannot see the danger -> KEEP IMMUNE.
  2. GROUNDED REACHABILITY (argument-driven only). Feed the function's real in-tree
     call sites + threat model to the auditor: can an attacker drive the crashing
     value? Only an explicit ``no`` demotes.

Fail-safe: anything uncertain / unparseable / channel-driven KEEPS immunity. The
guarantee is ZERO real-bug demotions — a missed false positive is cheap, a hidden
bug is not.

The decision is pure/advisory: ``grounded_immunity_decision`` returns
("keep"|"demote", reason). The caller chooses whether to act (shadow vs live).
"""
from __future__ import annotations

import glob
import json
import re
from pathlib import Path

from bmc_agent.logger import get_logger

logger = get_logger("reachability_grounding")


# ---------------------------------------------------------------------------
# Crash description (derived from the counterexample — production input)
# ---------------------------------------------------------------------------
def crash_summary(cex) -> str:
    """One-line description of the crash from the CBMC counterexample: the failing
    property plus the most salient nondet variable assignments (the values that
    drove the fault)."""
    prop = getattr(cex, "failing_property", "") or "unknown property"
    va = getattr(cex, "variable_assignments", None) or {}
    # Keep scalar-looking, top-level assignments (skip CPROVER builtins and the
    # huge array dumps); these are the values that characterise the crash.
    picks = []
    for k, v in va.items():
        ks = str(k)
        if ks.startswith("__") or "[" in ks or "." in ks:
            continue
        sv = str(v)
        if len(sv) > 40 or "array" in sv or "dynamic_object" in sv:
            continue
        picks.append(f"{ks}={sv}")
        if len(picks) >= 8:
            break
    state = (" Counterexample values: " + ", ".join(picks)) if picks else ""
    return f"CBMC reports a violation of '{prop}'.{state}"


# ---------------------------------------------------------------------------
# Mechanized call-site extraction (from the call graph + bodies)
# ---------------------------------------------------------------------------
def extract_call_sites(fn_name: str, all_funcs: dict, source_globs: "list[str] | None" = None) -> str:
    """Return the in-tree call sites of *fn_name* as raw lines (file:line: text).

    Primary source: the bodies in ``all_funcs`` (works cross-file within the run).
    Secondary: grep the project's .c files (catches callers not in all_funcs).
    No argument parsing — the raw call lines are the evidence the LLM reasons over.
    """
    sites: list[str] = []
    pat = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(fn_name) + r'\s*\(')
    defn = re.compile(r'\b' + re.escape(fn_name) + r'\s*\([^;]*\)\s*\{?\s*$')

    for caller, fi in (all_funcs or {}).items():
        if caller == fn_name:
            continue
        body = getattr(fi, "body", "") or ""
        for ln in body.splitlines():
            if pat.search(ln):
                sites.append(f"  (in {caller}): {ln.strip()[:120]}")

    if not sites and source_globs:
        for g in source_globs:
            for f in glob.glob(g):
                try:
                    lines = Path(f).read_text(errors="replace").splitlines()
                except OSError:
                    continue
                for i, ln in enumerate(lines, 1):
                    if pat.search(ln):
                        if defn.search(ln) and ("static" in ln or "{" in ln):
                            continue
                        sites.append(f"  {Path(f).name}:{i}: {ln.strip()[:120]}")

    if not sites:
        return "  (no in-tree call sites found — function may be an external/dispatch entry)"
    # de-dup, cap
    seen, out = set(), []
    for s in sites:
        if s not in seen:
            seen.add(s); out.append(s)
        if len(out) >= 14:
            break
    return "\n".join(out)


# ---------------------------------------------------------------------------
# LLM judgments
# ---------------------------------------------------------------------------
_ORIGIN_SYS = (
    "You analyze a C function crash reported by a bounded model checker. Decide where the "
    "value that CAUSES the crash (the faulting index / length / size / bounds / pointer "
    "offset) ORIGINATES. Answer ONLY JSON: {\"origin\":\"argument|internal\",\"why\":\"<=15 words\"}. "
    "argument = it comes directly from a scalar parameter the function receives. "
    "internal = it is READ from memory inside the body: a pointer's contents, a struct field "
    "reached through a pointer, a global variable, or an MMIO/device register read. "
    "When unsure, answer internal (the conservative, bug-preserving choice)."
)

_REACH_SYS = (
    "You are a security auditor for a bare-metal kernel. Given a crash and the function's REAL "
    "in-tree call sites plus the threat model, decide whether an ATTACKER can actually drive the "
    "function to the crashing state. Answer ONLY JSON: {\"attacker_reachable\":\"yes|no\",\"why\":\"<=20 words\"}. "
    "If the call sites show every real caller passes a constrained/internal value the attacker "
    "cannot influence, answer no. If any caller passes an attacker-controlled value (per the "
    "threat model) or you are unsure, answer yes."
)


def _ask_json(llm, system_prompt, user_prompt, key, cache_prefix=""):
    try:
        raw = llm.complete(system_prompt, user_prompt, role="realism",
                           temperature=0.0, max_tokens=200, cache_prefix=cache_prefix)
        d = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        return str(d.get(key, "")).strip().lower()
    except Exception as exc:
        logger.debug("reachability_grounding: parse/LLM failure (%s): %s", key, exc)
        return ""


def classify_crash_origin(body: str, crash: str, llm) -> str:
    """'argument' | 'internal' | 'uncertain'."""
    o = _ask_json(llm, _ORIGIN_SYS, f"FUNCTION:\n{body}\n\nCRASH:\n{crash}\n\nJSON only.", "origin")
    return o if o in ("argument", "internal") else "uncertain"


def grounded_reachable(body: str, crash: str, call_sites: str, threat: str, llm) -> str:
    """'yes' | 'no' | 'uncertain'."""
    u = (f"FUNCTION:\n{body}\n\nCRASH:\n{crash}\n\nREAL IN-TREE CALL SITES:\n{call_sites}\n\n"
         f"THREAT MODEL:\n{threat}\n\nJSON only.")
    r = _ask_json(llm, _REACH_SYS, u, "attacker_reachable")
    return r if r in ("yes", "no") else "uncertain"


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
_DEFAULT_THREAT = (
    "Attacker surface: bytes parsed from disk images / filesystems / ELF / DTB; syscall/trap "
    "pointer/length/index arguments; MMIO/DMA device input; and any value derived from these. "
    "Trusted (NOT attacker-controlled): kernel objects a caller allocates+initializes; "
    "internally-computed screen/layout coordinates."
)


def grounded_immunity_decision(func, cex, all_funcs, llm, *,
                               threat_context: str = "",
                               source_globs=None) -> "tuple[str, str]":
    """Decide whether confirmed_dynamic immunity should be SUSPENDED for this finding.

    Returns ("keep", reason) or ("demote", reason). Conservative/fail-safe: only an
    argument-driven crash whose grounded reachability is an explicit 'no' demotes;
    everything else (internal/channel-driven, uncertain, unparseable, no call sites)
    keeps immunity. Pure/advisory — the caller decides whether to act on it.
    """
    body = (getattr(func, "body", "") or "")[:1600]
    crash = crash_summary(cex)
    threat = threat_context.strip() or _DEFAULT_THREAT

    origin = classify_crash_origin(body, crash, llm)
    if origin != "argument":
        return "keep", f"channel-guard: crash origin '{origin}' (not a constrained argument) — grounding is blind to this channel; keep immune"

    sites = extract_call_sites(getattr(func, "name", ""), all_funcs, source_globs)
    if sites.startswith("  (no in-tree call sites"):
        return "keep", "no in-tree call sites to ground on (external/dispatch entry) — keep immune"

    reach = grounded_reachable(body, crash, sites, threat, llm)
    if reach == "no":
        return "demote", "arg-driven crash; grounded call sites show no real caller passes the attacker value — suspend immunity"
    return "keep", f"arg-driven but grounded reachability='{reach}' — keep immune (fail-safe)"
