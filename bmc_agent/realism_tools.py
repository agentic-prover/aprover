"""Tools for the realism check's bounded tool-use branch.

When the base realism check returns UNCERTAIN / UNREALISTIC, the LLM can
fetch additional context mid-reasoning rather than hallucinating a
caller-chain story. Three tools:

  * walk_call_chain(fn_name, max_depth) — verified call graph from the
    parsed corpus; for each level returns (caller, established_PRE).
    The LLM uses this to judge reachability against ground-truth call
    structure instead of imagining one.
  * lookup_function(name) — full body of a callee so the LLM can find
    NULL guards / range checks that wouldn't be visible from the
    summary in the realism prompt (this is exactly what would have
    killed the v7 cmp_key_mbs FP: the judge could read into
    archive_mstring_copy_wcs_len and see the explicit NULL guard).
  * lookup_callee_postcondition(name) — what a stubbed callee
    promised on return. Lets the LLM check whether the witness state
    is even consistent with the callee's contract.

Soundness: handlers return deterministic data from already-parsed
corpus + already-generated specs. The LLM's verdict (REALISTIC /
UNREALISTIC / UNCERTAIN) is what gets reported; its tool-supported
reasoning is grounded in mechanical evidence.

This module's walk_call_chain is intentionally conservative — it
returns the call graph + each caller's spec PRE, NOT a CBMC-formal
reachability assertion. Full integration with
cex_validator._check_caller_reachability is a follow-up; the current
data shape is enough to break the confabulation pattern (LLM
inventing chains that don't exist in the graph).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from bmc_agent.llm import ToolDef

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.spec import Spec


# ---------- per-realism-check tool context ---------------------------------


@dataclass
class RealismToolContext:
    """Context handed to realism tool handlers. One per realism check call."""

    parsed: "ParsedCFile"
    all_specs: "dict[str, Spec]"
    # Cross-file callers map: fn_name → list of (caller_name, file:line)
    # When populated, walk_call_chain can traverse beyond the single-file
    # parsed corpus. When empty, it only walks intra-file callers.
    cross_file_callers: "Optional[dict[str, list[tuple[str, str]]]]" = None


# ---------- handler caps ---------------------------------------------------

_MAX_CHAIN_DEPTH = 5
_MAX_CHAIN_BRANCH = 6        # max callers per node
_MAX_FUNCTION_BODY_CHARS = 4000


# ---------- handler factories ---------------------------------------------


def _make_walk_call_chain(ctx: RealismToolContext) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        target = str(args.get("fn_name") or args.get("name") or "").strip()
        max_depth = int(args.get("max_depth") or 3)
        max_depth = max(1, min(max_depth, _MAX_CHAIN_DEPTH))
        if not target:
            return {"error": "missing 'fn_name' argument"}
        # Reverse call graph (callee → callers) from parsed.
        # parsed.call_graph is {caller: set(callees)}; invert.
        callers_of: dict[str, list[str]] = {}
        for caller, callees in (ctx.parsed.call_graph or {}).items():
            for c in callees:
                callers_of.setdefault(c, []).append(caller)
        # BFS chain build.
        chain: list[dict] = []
        frontier = [target]
        seen: set[str] = {target}
        for depth in range(1, max_depth + 1):
            next_frontier: list[str] = []
            level_entries: list[dict] = []
            for fn in frontier:
                callers = sorted(callers_of.get(fn, []))[:_MAX_CHAIN_BRANCH]
                for caller in callers:
                    if caller in seen:
                        continue
                    seen.add(caller)
                    caller_spec = ctx.all_specs.get(caller)
                    pre = caller_spec.precondition if caller_spec else ""
                    level_entries.append({
                        "callee": fn,
                        "caller": caller,
                        "caller_pre": pre or "(no spec yet)",
                    })
                    next_frontier.append(caller)
            if not level_entries:
                break
            chain.append({"depth": depth, "edges": level_entries})
            frontier = next_frontier
        # If chain is empty, the function has no callers in the visible
        # corpus — vtable-only or dead code. Report it explicitly so the
        # LLM can factor that into the realism judgment.
        if not chain:
            return {
                "target": target,
                "chain": [],
                "note": (
                    "no callers in visible corpus — function is either "
                    "reached only via function-pointer dispatch (vtable / "
                    "callback registration) or dead code"
                ),
            }
        return {"target": target, "chain": chain, "max_depth_reached": len(chain)}
    return handler


def _make_lookup_function(ctx: RealismToolContext) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "missing 'name' argument"}
        fi = ctx.parsed.get_function_info(name)
        if fi is None:
            return {"error": f"function '{name}' not in parsed corpus"}
        sig = fi.signature
        return {
            "name": name,
            "return_type": sig.return_type,
            "parameters": [{"type": t, "name": n} for t, n in sig.parameters],
            "source_file": getattr(fi, "source_file", "") or "",
            "callees": sorted(getattr(fi, "callees", set())),
            "body": (fi.body or "")[:_MAX_FUNCTION_BODY_CHARS],
            "body_truncated": len(fi.body or "") > _MAX_FUNCTION_BODY_CHARS,
        }
    return handler


def _make_lookup_callee_postcondition(
    ctx: RealismToolContext,
) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "missing 'name' argument"}
        spec = ctx.all_specs.get(name)
        if spec is None:
            return {
                "error": (
                    f"no spec for '{name}' — callee either external "
                    "(stubbed by universal_stub_contracts if applicable) "
                    "or not yet generated"
                ),
            }
        return {
            "name": name,
            "precondition": spec.precondition,
            "postcondition": spec.postcondition,
            "pre_validity": spec.pre_validity or "",
            "pre_protocol": spec.pre_protocol or "",
            "evidence_tags": list(spec.evidence.keys())[:20],
            "status": spec.status.value if hasattr(spec.status, "value") else str(spec.status),
        }
    return handler


# ---------- public entry point --------------------------------------------


def build_realism_tools(
    ctx: RealismToolContext,
) -> tuple[list[ToolDef], dict[str, Callable[[dict], object]]]:
    """Return (tool_defs, handlers) for the realism check's tool-use branch.

    Pass the result straight into ``LLMClient.complete_with_tools``.
    """
    tools = [
        ToolDef(
            name="walk_call_chain",
            description=(
                "Walk the call graph UPWARD from the function under realism "
                "check, returning each level of callers and their PRE "
                "constraints. Use to verify whether any actual caller can "
                "establish the CEx witness state — beats hallucinating a "
                "caller-chain story. Returns 'no callers' explicitly when "
                "the function is vtable-only or dead code."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fn_name": {"type": "string"},
                    "max_depth": {"type": "integer",
                                  "description": f"max depth, 1-{_MAX_CHAIN_DEPTH} (default 3)"},
                },
                "required": ["fn_name"],
            },
        ),
        ToolDef(
            name="lookup_function",
            description=(
                "Get the full body of a callee so you can find NULL guards "
                "or range checks that the realism prompt's summary missed. "
                "Use when the CEx claims a callee dereferences a NULL field "
                "but the callee may actually guard against it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        ),
        ToolDef(
            name="lookup_callee_postcondition",
            description=(
                "Get a callee's spec POST. Use to check whether the CEx "
                "witness state is even consistent with the callee's "
                "documented contract — if the POST says result != NULL and "
                "the witness has result == NULL, the CEx requires the "
                "callee to have violated its own POST."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        ),
    ]
    handlers: dict[str, Callable[[dict], object]] = {
        "walk_call_chain":              _make_walk_call_chain(ctx),
        "lookup_function":              _make_lookup_function(ctx),
        "lookup_callee_postcondition":  _make_lookup_callee_postcondition(ctx),
    }
    return tools, handlers


# ---------- prompt addendum -----------------------------------------------

TOOL_USE_PROMPT_ADDENDUM = """

You have access to tools for grounding your realism judgment in
mechanical evidence rather than narrative reasoning:

  * `walk_call_chain(fn_name)` — verified call graph from the corpus.
    Use this BEFORE claiming "an attacker reaches this via some
    upstream caller." If the chain is empty or none of the callers
    can establish the witness state, the CEx is UNREALISTIC.
  * `lookup_function(name)` — read a function's full body. Use to
    check whether a guard you claimed is missing (NULL check, range
    check) is actually missing from the source. The function body
    returned is the ground truth — if you claim "no NULL check before
    strcmp(p, name)" but lookup_function shows `if (p != NULL && strcmp(p, name) == 0)`,
    your reasoning is wrong and the CEx is UNREALISTIC.
  * `lookup_callee_postcondition(name)` — verify the witness against
    the callee's documented POST. A CEx that requires the callee to
    violate its own POST is UNREALISTIC (unless the realism question
    is about the callee, not the FUT).

================================================================
HARD REQUIREMENTS (failure to follow = invalid verdict)
================================================================

(1) BEFORE producing your verdict, you MUST call
    `lookup_function(name=<the function under realism check>)`
    to read the actual function body. The function summary in
    the base prompt is truncated/condensed; only the body
    returned by this tool is authoritative.

(2) When your reasoning claims an unguarded operation at line L
    (e.g., "strcmp(p, name) called without NULL check"), you MUST:

      (a) Quote the LITERAL source line from the body returned
          by lookup_function, verbatim.
      (b) Confirm the quoted line does NOT contain a guard for
          the operand (e.g., no `if (p != NULL &&`, no
          `if (p)`, no `if (!p) return`).
      (c) Confirm the operand is not guarded in the surrounding
          lines (3 lines above) — short-circuit guards like
          `if (p != NULL && strcmp(p, name) == 0)` are common.

    If you cannot satisfy (a)/(b)/(c), the CEx is UNREALISTIC.

(3) Your final JSON verdict MUST include a `grounding` field:

      "grounding": {
        "looked_up_target_body": true,
        "quoted_line": "<the literal line from the body, verbatim>",
        "guard_search_result": "<one of: 'no guard found' | 'guard present: <quote>' >"
      }

    If `guard_search_result` indicates a guard IS present, your
    verdict MUST be UNREALISTIC.

================================================================

You may make up to 3 tool calls. Stop and emit the JSON verdict when
you have enough evidence. The same OUTPUT FORMAT from the base prompt
applies (verdict / reasoning / key_concern / confidence) — PLUS the
`grounding` field above.
"""
