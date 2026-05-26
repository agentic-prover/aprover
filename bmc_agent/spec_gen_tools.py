"""Tools for the v2.2 spec generator's bounded tool-use branch.

When the base v2 spec gen flags a function as spec_disagreement (body
and observed callers contradict) OR when there's no caller evidence at
all (vtable-only / orphan functions), v2.2 fires a second LLM call —
this time with tool access — so the LLM can fetch authoritative data
mid-reasoning rather than being capped at the bundled context.

Each tool here is a small handler that returns deterministic data from
the parsed corpus or already-generated specs. No LLM in the handler;
the LLM's output (a spec proposal) is what gets verified downstream by
CBMC. That's the load-bearing property: tool use is safe because the
verifier is external.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from bmc_agent.llm import ToolDef

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bmc_agent.boundary_detector import BoundaryDetector
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.spec import Spec


# ---------- per-spec-generation tool context --------------------------------


@dataclass
class SpecToolContext:
    """Context handed to spec-gen tool handlers.

    Holds references to the parsed corpus, already-generated specs, and
    the boundary detector so handlers can answer queries without re-doing
    work. One context per function-being-spec'd.
    """

    parsed: "ParsedCFile"
    corpus_paths: list[Path]
    all_specs_so_far: "dict[str, Spec]"
    boundary_detector: Optional["BoundaryDetector"] = None


# ---------- handler factories -----------------------------------------------


# Hard caps on per-tool-call result sizes (tool-use loop also truncates,
# but emitting big blobs wastes tokens for everyone).
_MAX_FUNCTION_BODY_CHARS = 4000
_MAX_CALLER_CONTEXT_LINES = 12
_MAX_GREP_MATCHES = 8


def _make_lookup_function(ctx: SpecToolContext) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "missing 'name' argument"}
        fi = ctx.parsed.get_function_info(name)
        if fi is None:
            return {"error": f"function '{name}' not found in parsed corpus"}
        sig = fi.signature
        params = [{"type": t, "name": n} for t, n in sig.parameters]
        return {
            "name": name,
            "return_type": sig.return_type,
            "parameters": params,
            "is_static": getattr(sig, "is_static", False),
            "source_file": getattr(fi, "source_file", "") or "",
            "callees": sorted(getattr(fi, "callees", set())),
            "body": (fi.body or "")[:_MAX_FUNCTION_BODY_CHARS],
            "body_truncated": len(fi.body or "") > _MAX_FUNCTION_BODY_CHARS,
        }
    return handler


def _make_find_more_callers(ctx: SpecToolContext) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        k = int(args.get("k") or 10)
        if not name:
            return {"error": "missing 'name' argument"}
        from bmc_agent.spec_evidence import harvest_callers
        try:
            hits = harvest_callers(
                name, ctx.corpus_paths, k=max(1, min(k, 25)),
                context_radius=_MAX_CALLER_CONTEXT_LINES // 2,
                candidate_fn_names=set(ctx.parsed.functions.keys()),
            )
        except Exception as exc:
            return {"error": f"caller harvest failed: {type(exc).__name__}: {exc}"}
        if not hits:
            # Fall back to address-taken sites if no direct callers.
            from bmc_agent.spec_evidence import harvest_address_taken_sites
            addr_hits = harvest_address_taken_sites(
                name, ctx.corpus_paths, k=3,
            )
            return {
                "direct_callers": [],
                "address_taken_sites": [
                    {"file": h.file, "line": h.line,
                     "context": h.context_lines}
                    for h in addr_hits
                ],
            }
        return {
            "direct_callers": [
                {
                    "file": h.file,
                    "line": h.line,
                    "enclosing_function": h.enclosing_function,
                    "context": h.context_lines,
                }
                for h in hits
            ],
        }
    return handler


def _make_lookup_struct(ctx: SpecToolContext) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        tag = str(args.get("tag") or "").strip()
        if not tag:
            return {"error": "missing 'tag' argument"}
        struct_defs = getattr(ctx.parsed, "struct_definitions", {}) or {}
        fields = struct_defs.get(tag)
        if fields is None:
            return {"error": f"struct '{tag}' not in parsed corpus's struct table"}
        return {
            "tag": tag,
            "fields": [{"type": ft, "name": fn} for ft, fn in fields],
            "field_count": len(fields),
        }
    return handler


def _make_lookup_caller_spec(ctx: SpecToolContext) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "missing 'name' argument"}
        spec = ctx.all_specs_so_far.get(name)
        if spec is None:
            return {"error": f"no spec yet for '{name}' (not generated, or generated after)"}
        try:
            return spec.to_dict()
        except Exception as exc:
            return {"error": f"spec serialization failed: {type(exc).__name__}: {exc}"}
    return handler


def _make_grep_corpus(ctx: SpecToolContext) -> Callable[[dict], dict]:
    def handler(args: dict) -> dict:
        pattern = str(args.get("pattern") or "").strip()
        k = int(args.get("k") or _MAX_GREP_MATCHES)
        if not pattern:
            return {"error": "missing 'pattern' argument"}
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"error": f"invalid regex: {exc}"}
        matches: list[dict] = []
        for p in ctx.corpus_paths:
            try:
                lines = Path(p).read_text(errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if rx.search(line):
                    matches.append({
                        "file": str(p),
                        "line": i + 1,
                        "snippet": line[:200],
                    })
                    if len(matches) >= k:
                        return {"matches": matches}
        return {"matches": matches}
    return handler


# ---------- public entry point ---------------------------------------------


def build_spec_gen_tools(
    ctx: SpecToolContext,
) -> tuple[list[ToolDef], dict[str, Callable[[dict], object]]]:
    """Return (tool_defs, handlers) for the v2.2 spec gen tool-use branch.

    The handlers close over ``ctx`` so the LLM gets corpus-local answers.
    Pass the result straight into ``LLMClient.complete_with_tools``.
    """
    tools = [
        ToolDef(
            name="lookup_function",
            description=(
                "Get the full signature, body, source file, and callees "
                "of a function defined in the parsed corpus. Use this "
                "when you need to verify a callee's actual behavior to "
                "refine the PRE."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "function name (no parens)"},
                },
                "required": ["name"],
            },
        ),
        ToolDef(
            name="find_more_callers",
            description=(
                "Find up to N callers of a function in the corpus, beyond "
                "the small sample bundled in the prompt. Returns each call "
                "site's file, line, and surrounding context. Falls back to "
                "address-taken sites (vtable / callback registration) when "
                "no direct callers exist."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "k": {"type": "integer",
                          "description": "max results (default 10, cap 25)"},
                },
                "required": ["name"],
            },
        ),
        ToolDef(
            name="lookup_struct",
            description=(
                "Get the field list of a struct type defined in the parsed "
                "corpus. Use this when a parameter's struct shape matters "
                "for a field-level !null clause."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tag": {"type": "string",
                            "description": "struct tag name (no 'struct ' prefix)"},
                },
                "required": ["tag"],
            },
        ),
        ToolDef(
            name="lookup_caller_spec",
            description=(
                "If a caller of the function-under-spec has already been "
                "spec'd in this sweep (bottom-up topological order means "
                "many will be), return its current Spec. Use this to "
                "verify what guarantees the caller offers."
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
            name="grep_corpus",
            description=(
                "Grep a regex pattern across all corpus files; return up "
                "to N matches with file/line/snippet. Use to find adjacent "
                "patterns, initialization sites, or related functions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string",
                                "description": "Python regex"},
                    "k": {"type": "integer",
                          "description": "max matches (default 8)"},
                },
                "required": ["pattern"],
            },
        ),
    ]
    handlers: dict[str, Callable[[dict], object]] = {
        "lookup_function":     _make_lookup_function(ctx),
        "find_more_callers":   _make_find_more_callers(ctx),
        "lookup_struct":       _make_lookup_struct(ctx),
        "lookup_caller_spec":  _make_lookup_caller_spec(ctx),
        "grep_corpus":         _make_grep_corpus(ctx),
    }
    return tools, handlers


# ---------- tool-use system prompt addendum --------------------------------

TOOL_USE_PROMPT_ADDENDUM = """

You have access to tools for fetching additional context mid-reasoning.
Use them when the bundled context isn't enough — specifically:

  * `lookup_function(name)` when you need to verify what a callee
    actually does (NULL-guards, return-value constraints) before
    committing to a PRE clause that depends on it
  * `find_more_callers(name)` when the sampled callers contradict the
    body and you want to see if the contradiction is real across the
    broader caller set
  * `lookup_struct(tag)` when a struct parameter's field shape matters
    for emitting field-level !null clauses
  * `lookup_caller_spec(name)` when a caller has already been spec'd
    and its PRE/POST would constrain what state can reach you
  * `grep_corpus(pattern)` for adjacent-function discovery (find all
    functions in `*_init` family, etc.)

You may make up to 5 tool calls. Stop and emit the JSON spec when you
have enough evidence; using tools when you don't need to wastes budget.

The same OUTPUT FORMAT and HARD RULES from the base prompt apply to
your final answer.
"""
