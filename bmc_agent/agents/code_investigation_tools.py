"""Shared, self-contained code-investigation tools for agentic agent variants.

Several agents (refinement, feedback_distill, classifier) reason better when they
can PULL the real code on demand — read a function, grep for a caller, inspect a
struct — instead of judging from a fixed prompt. Unlike spec_gen_tools (which
needs a parsed corpus), these tools operate directly on a list of source file
paths, so any agent can use them with zero pipeline plumbing: just hand it the
``--source`` + ``--include-dir`` files.

Tools (all read-only, bounded):
  - grep_code(pattern):        ripgrep-style search across the files (path:line:text)
  - read_lines(file, start, end): read a line range of one file
  - read_function(name):       extract a C function definition by name

Returns (tools, handlers) ready for ``LLMClient.complete_with_tools``.
"""

from __future__ import annotations

import os
import re
from typing import Callable

from bmc_agent.llm import ToolDef

_MAX_OUT = 6000          # truncate any tool result to keep turns bounded
_MAX_HITS = 40


def _expand(paths: "list[str]") -> "list[str]":
    """Resolve the path list to existing .c/.h files (dirs are globbed)."""
    out: list[str] = []
    seen: set = set()
    for p in paths or []:
        if not p:
            continue
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    if f.endswith((".c", ".h")):
                        fp = os.path.join(root, f)
                        if fp not in seen:
                            seen.add(fp); out.append(fp)
        elif os.path.isfile(p) and p not in seen:
            seen.add(p); out.append(p)
    return out


def _truncate(s: str) -> str:
    return s if len(s) <= _MAX_OUT else s[:_MAX_OUT] + "\n...[truncated]"


def _make_grep(files: "list[str]") -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        pat = str(args.get("pattern", "")).strip()
        if not pat:
            return "ERROR: missing 'pattern'"
        try:
            rx = re.compile(pat)
        except re.error as exc:
            return f"ERROR: bad regex: {exc}"
        hits: list[str] = []
        for fp in files:
            try:
                with open(fp, "r", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{os.path.basename(fp)}:{i}: {line.rstrip()}")
                            if len(hits) >= _MAX_HITS:
                                hits.append("...[more hits truncated]")
                                return _truncate("\n".join(hits))
            except OSError:
                continue
        return _truncate("\n".join(hits)) if hits else "(no matches)"
    return handler


def _make_read_lines(files: "list[str]") -> Callable[[dict], str]:
    by_base = {os.path.basename(f): f for f in files}
    def handler(args: dict) -> str:
        fname = str(args.get("file", "")).strip()
        start = int(args.get("start", 1) or 1)
        end = int(args.get("end", start + 60) or start + 60)
        fp = by_base.get(os.path.basename(fname)) or fname
        if not os.path.isfile(fp):
            return f"ERROR: file not found: {fname} (known: {', '.join(sorted(by_base)[:20])})"
        try:
            with open(fp, "r", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return f"ERROR: {exc}"
        start = max(1, start); end = min(len(lines), max(start, end))
        body = "".join(f"{i}: {lines[i-1]}" for i in range(start, end + 1))
        return _truncate(body) or "(empty range)"
    return handler


def _make_read_function(files: "list[str]") -> Callable[[dict], str]:
    def handler(args: dict) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "ERROR: missing 'name'"
        # crude C function-definition finder: a line containing `name(` that is
        # not a call (heuristic: starts at column 0 or after a type), then
        # brace-match to the closing brace.
        rx = re.compile(r"(^|\b)[A-Za-z_][\w \*]*\b" + re.escape(name) + r"\s*\(")
        for fp in files:
            try:
                with open(fp, "r", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if rx.search(line) and "{" in "".join(lines[i:i+8]):
                    depth = 0; started = False; out = []
                    for j in range(i, min(len(lines), i + 400)):
                        out.append(f"{j+1}: {lines[j]}")
                        depth += lines[j].count("{") - lines[j].count("}")
                        if "{" in lines[j]:
                            started = True
                        if started and depth <= 0:
                            return _truncate(f"// {os.path.basename(fp)}\n" + "".join(out))
        return f"(no definition found for {name})"
    return handler


def build_code_tools(source_paths: "list[str]") -> "tuple[list[ToolDef], dict]":
    """Build (tools, handlers) over the given source files/dirs."""
    files = _expand(source_paths)
    tools = [
        ToolDef(name="grep_code",
                description="Search the project C/H sources for a regex; returns file:line: matches.",
                parameters={"type": "object",
                            "properties": {"pattern": {"type": "string", "description": "Python regex"}},
                            "required": ["pattern"]}),
        ToolDef(name="read_lines",
                description="Read a line range of one source file (by basename).",
                parameters={"type": "object",
                            "properties": {"file": {"type": "string"},
                                           "start": {"type": "integer"},
                                           "end": {"type": "integer"}},
                            "required": ["file", "start", "end"]}),
        ToolDef(name="read_function",
                description="Extract a C function definition by name (body included).",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"]}),
    ]
    handlers = {
        "grep_code": _make_grep(files),
        "read_lines": _make_read_lines(files),
        "read_function": _make_read_function(files),
    }
    return tools, handlers


class CodeToolsCallMixin:
    """Mixin that turns a flat BaseAgent into an in-process tool-using (agentic)
    one: it can grep/read the real source to ground its judgment. Keeps the
    host agent's build_prompt/parse/system_prompt unchanged — only _call_llm is
    overridden to run a bounded tool loop (via the anthropic-native or openai
    complete_with_tools path). Falls back to the flat call when the agent is
    routed to the claude-code backend.
    """

    max_iterations_param = 6
    max_tool_calls_param = 8
    max_tokens_per_turn_param = 4096

    def _code_tool_paths(self) -> "list[str]":
        paths = list(getattr(self.config, "include_dirs", []) or [])
        src = getattr(self.config, "source_path", "") or getattr(self.config, "source", "")
        if src:
            paths = [src] + paths
        return paths

    def _call_llm(self, prompt: str):
        if self._agent_runs_on_claude_code():
            return super()._call_llm(prompt)
        from bmc_agent.llm import LLMError
        tools, handlers = build_code_tools(self._code_tool_paths())
        try:
            result = self.llm.complete_with_tools(
                system_prompt=self.system_prompt,
                user_prompt=prompt,
                tools=tools,
                tool_handlers=handlers,
                max_iterations=self.max_iterations_param,
                max_tool_calls=self.max_tool_calls_param,
                max_tokens_per_turn=self.max_tokens_per_turn_param,
                role=self.name,
            )
        except LLMError as exc:
            return "", f"LLMError: {exc!r}"
        except Exception as exc:  # noqa: BLE001
            return "", f"unexpected: {exc!r}"
        self._last_tool_use_result = result
        if result.error:
            return result.text or "", f"tool_use_terminated: {result.error}"
        return result.text or "", None
