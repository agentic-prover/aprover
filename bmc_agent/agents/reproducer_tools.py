"""``ReproducerAgent`` - tool-using, compile-and-run reproducer synthesis.

The legacy reproducer path is a ONE-SHOT LLM call (``scenario_reproducer``
+ ``DynamicReproAgent``): the model emits one C program; if it doesn't
compile or doesn't reproduce, the finding "falls open" to UNREPRODUCIBLE
even when all it needed was a missing header, a wrong struct field, or a
mis-typed argument. Real bugs are lost to a trivial build error.

This agent fixes that with a BOUNDED tool loop. The model gets the same
investigation tools as ``TriageToolsAgent`` (``lookup_function``,
``find_more_callers``, ``lookup_struct``, ``grep_corpus``) PLUS one extra
tool, ``compile_and_run_reproducer(source)``, which actually compiles the
candidate with the configured C compiler and runs it, feeding the
compiler / runtime error text back into the next turn. So the model
iterates compile -> run -> read-error -> fix until the reproducer both
COMPILES and CRASHES with the right fault class, or the budget runs out.

Output contract matches ``DynamicReproAgent`` (role/name ``dynamic_repro``
so per-role routing env vars keep working): the parsed output is a C
source string, and the UNREPRODUCIBLE sentinel ``// UNREPRODUCIBLE: ...``
is honoured verbatim by the caller's outer loop as a graceful give-up.

The PUBLIC-API guard (``_reproducer_uses_public_api`` from
``cex_validator``) is reused, not re-implemented: a reproducer that does
not include a real project public header (i.e. fabricates a wrong-reason
crash by re-implementing internal helpers) is rejected and reported as
UNREPRODUCIBLE.

Fail-safe: any error inside the tool loop becomes UNREPRODUCIBLE; nothing
raises into the caller.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any, Optional

from bmc_agent.agents.base import BaseAgent

if TYPE_CHECKING:
    from pathlib import Path

    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import ParsedCFile
    from bmc_agent.spec import Spec

logger = logging.getLogger(__name__)


#: Honoured verbatim by the caller's outer loop (mirrors DynamicReproAgent
#: + scenario_reproducer): an UNREPRODUCIBLE-marked source is a graceful
#: give-up, not a hard error.
UNREPRODUCIBLE_SENTINEL = "// UNREPRODUCIBLE: reproducer agent could not compile-and-reproduce via the public API"


_SYSTEM_PROMPT = (
    "You are a C exploit-reproduction expert. You are given a CBMC "
    "counterexample for a buggy function, the CBMC property that failed, "
    "and the call chain to the public API. Your job: produce a "
    "SELF-CONTAINED C reproducer that:\n\n"
    "  1. Drives the function to the faulting state described by the\n"
    "     counterexample and the CBMC property, using ONLY the real\n"
    "     PUBLIC API of the project (the published headers). Do NOT\n"
    "     fabricate a crash by calling an internal helper with\n"
    "     hand-rigged state, and do NOT re-implement project functions\n"
    "     or copy opaque structs inline - that proves nothing about the\n"
    "     real library.\n"
    "  2. COMPILES and, when run, CRASHES with the SAME fault class as\n"
    "     the property (e.g. SIGSEGV for an out-of-bounds pointer\n"
    "     dereference, an AddressSanitizer OOB report for a heap\n"
    "     overflow, SIGFPE for a div-by-zero). A crash for a DIFFERENT\n"
    "     reason does not count.\n"
    "  3. Is MINIMAL - only the includes and calls needed to reach the\n"
    "     fault.\n\n"
    "TOOLS - use them, do not guess:\n"
    "  * lookup_function(name) - read the real signature and body of any\n"
    "    function and the call chain up to the public entry point, so\n"
    "    your calls and argument types are correct.\n"
    "  * lookup_struct(tag) - exact struct field layout, so your types\n"
    "    compile.\n"
    "  * grep_corpus(pattern, k) - find the right header, macro, or enum\n"
    "    (e.g. the public-API include, a flag constant).\n"
    "  * find_more_callers(name, k) - discover the public-API path that\n"
    "    reaches the buggy function.\n"
    "  * compile_and_run_reproducer(source) - COMPILE your candidate C\n"
    "    source and RUN it. Returns 'OK: reproduced ...' on a matching\n"
    "    crash, or the compiler / runtime error text otherwise.\n\n"
    "ITERATE: after each compile_and_run_reproducer call you will see the\n"
    "error. FIX it (add the missing #include, correct the wrong type,\n"
    "fix the argument) and call compile_and_run_reproducer again. Repeat\n"
    "the compile -> run -> read-error -> fix loop until it both COMPILES\n"
    "and REPRODUCES the fault via the public API.\n\n"
    "When you have a reproducer that compile_and_run_reproducer confirmed\n"
    "(returned 'OK: reproduced ...'), emit it as your final answer in a\n"
    "single fenced ```c code block and nothing else.\n\n"
    "If after your budget you cannot make it BOTH compile AND reproduce\n"
    "through the real public API, emit exactly the line:\n"
    "  // UNREPRODUCIBLE: <one-line reason>\n"
    "and nothing else. Do NOT emit a reproducer that only calls internal\n"
    "helpers - that will be rejected as UNREPRODUCIBLE anyway."
)


# Defensive compile/run knobs for the compile_and_run tool. Kept local so
# this agent never depends on the DynamicValidator instance being wired up.
_COMPILE_TIMEOUT_S = 30
_RUN_TIMEOUT_S = 15


class ReproducerAgent(BaseAgent[str]):
    """Tool-using reproducer synthesis.

    Public contract (matches ``DynamicReproAgent``):

      * ``name = "dynamic_repro"`` so BMC_AGENT_LLM_DYNAMIC_REPRO_* routing
        and the caller's marker handling are unchanged.
      * ``run(...)`` returns ``AgentResult[str]``; ``output`` is the C
        reproducer source on success, or the UNREPRODUCIBLE sentinel
        string (``// UNREPRODUCIBLE: ...``) when no reproducer could be
        built through the public API. ``output is None`` only when the LLM
        produced an empty response (BaseAgent then reports an error and
        the caller falls back to the prior reproducer).
    """

    name = "dynamic_repro"
    system_prompt = _SYSTEM_PROMPT

    #: Reproduction is the most iterative task - give it the largest
    #: compile/run loop budget of any tool agent.
    max_iterations_param: int = 12
    max_tool_calls_param: int = 12
    max_tokens_per_turn_param: int = 4096

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        *,
        parsed_file: "ParsedCFile",
        corpus_paths: "list[Path]",
        all_specs: "Optional[dict[str, Spec]]" = None,
    ) -> None:
        self.parsed_file = parsed_file
        self.corpus_paths = list(corpus_paths)
        self.all_specs = dict(all_specs or {})
        super().__init__(config, llm)
        self._last_tool_use_result = None
        #: Public-header allowlist for the public-API guard; resolved lazily
        #: from config.include_dirs in ``_public_headers``.
        self._public_headers_cache: Optional[list[str]] = None

    def _llm_call_kwargs(self) -> dict:
        return {}  # tool-use loop manages its own per-turn budget

    # ------------------------------------------------------------------
    # Public-API guard plumbing (reused, never re-implemented)
    # ------------------------------------------------------------------

    def _public_headers(self) -> Optional[list[str]]:
        """Project public-header allowlist for the guard, autodiscovered
        from ``config.include_dirs``. Returns None when nothing is found so
        ``_reproducer_uses_public_api`` falls back to its built-in set."""
        if self._public_headers_cache is not None:
            return self._public_headers_cache or None
        try:
            from bmc_agent.cex_validator import _autodiscover_public_headers
            include_dirs = [str(d) for d in (getattr(self.config, "include_dirs", None) or [])]
            self._public_headers_cache = _autodiscover_public_headers(include_dirs)
        except Exception:
            self._public_headers_cache = []
        return self._public_headers_cache or None

    def _uses_public_api(self, source: str) -> bool:
        """Reuse the canonical guard from cex_validator. Defensive: on any
        import/lookup error treat the source as NOT public-API (safer to
        report UNREPRODUCIBLE than to confirm a fabricated crash)."""
        try:
            from bmc_agent.cex_validator import _reproducer_uses_public_api
            return _reproducer_uses_public_api(source, self._public_headers())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # build_prompt
    # ------------------------------------------------------------------

    def build_prompt(
        self,
        *,
        function_name: str,
        cbmc_property: str,
        counterexample: str,
        call_chain: Any = None,
        function_source: str = "",
        public_api_hint: Optional[str] = None,
        threat_context: Optional[str] = None,
        **_: Any,
    ) -> str:
        chain_txt = self._render_call_chain(call_chain)
        parts = [
            f"BUGGY FUNCTION: {function_name}",
            "",
            "=== CBMC PROPERTY THAT FAILED (the fault class to reproduce) ===",
            (cbmc_property or "(unspecified)").strip(),
            "",
            "=== COUNTEREXAMPLE (variable assignments at the fault) ===",
            (counterexample or "(no counterexample text)").strip()[:6000],
            "",
            "=== CALL CHAIN TO A PUBLIC-API ENTRY POINT ===",
            chain_txt,
        ]
        if public_api_hint:
            parts += [
                "",
                "=== PUBLIC-API HINT ===",
                str(public_api_hint).strip()[:1500],
            ]
        if threat_context:
            parts += [
                "",
                "=== THREAT-MODEL CONTEXT ===",
                str(threat_context).strip()[:1500],
            ]
        parts += [
            "",
            "=== FUNCTION SOURCE (for reference) ===",
            "```c",
            (function_source or "(body unavailable)")[:6000],
            "```",
            "",
            "Build the reproducer now. Use the tools to read the real "
            "signatures / struct layouts / headers, call "
            "compile_and_run_reproducer to compile-and-run each candidate, "
            "and FIX the reported errors until it both compiles and "
            "reproduces the fault via the public API. Then emit the final "
            "reproducer in a single fenced ```c block, or the "
            "// UNREPRODUCIBLE: line if you cannot.",
        ]
        return "\n".join(parts)

    @staticmethod
    def _render_call_chain(call_chain: Any) -> str:
        if not call_chain:
            return "(no call chain provided)"
        if isinstance(call_chain, str):
            return call_chain.strip()[:3000]
        try:
            return " -> ".join(str(x) for x in call_chain)[:3000]
        except Exception:
            return str(call_chain)[:3000]

    # ------------------------------------------------------------------
    # compile_and_run tool
    # ------------------------------------------------------------------

    def _compile_and_run_source(self, source: str) -> str:
        """COMPILE then RUN ``source`` with the configured C compiler.

        Returns a short status string for the LLM:
          * 'OK: reproduced <signal/sanitizer>' when the program crashes
            (negative return = killed by signal, sanitizer report on
            stderr, or 128< exit code).
          * 'COMPILE ERROR:\\n<stderr>' on a build failure.
          * 'RAN OK (exit 0) - no crash: <stdout/stderr tail>' when it
            built and ran cleanly (NOT a reproduction).
          * 'RUN ERROR: <reason>' on timeout / launch failure.

        Defensive: cleans up temp files; never raises (returns an error
        string instead, so the tool loop keeps going)."""
        src_path = None
        bin_path = None
        try:
            cc = getattr(self.config, "dynamic_cc_path", None) or "gcc"
            with tempfile.NamedTemporaryFile(
                suffix=".c", delete=False, mode="w", encoding="utf-8"
            ) as src_f:
                src_f.write(source or "")
                src_path = src_f.name
            with tempfile.NamedTemporaryFile(suffix="", delete=False) as bin_f:
                bin_path = bin_f.name

            cmd = [
                cc, "-O0", "-g",
                "-fsanitize=address,undefined",
                "-fno-omit-frame-pointer",
                "-w",
                "-o", bin_path, src_path,
            ]
            # Propagate the project -I / -D so a public-API reproducer can
            # find the project headers. Skip stub-libc include dirs (they
            # shadow the real <signal.h>/<stdio.h> on freestanding targets),
            # mirroring DynamicValidator._compile.
            for d in (getattr(self.config, "include_dirs", None) or []):
                d = str(d)
                if self._is_stub_libc_dir(d):
                    continue
                cmd += ["-I", d]
            for define in (getattr(self.config, "cbmc_defines", None) or []):
                cmd += ["-D", str(define)]

            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=_COMPILE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return f"COMPILE ERROR:\ngcc timed out (>{_COMPILE_TIMEOUT_S}s)"
            except Exception as exc:
                return f"COMPILE ERROR:\n{exc!r}"

            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()[:1500]
                return "COMPILE ERROR:\n" + (err or "(no compiler output)")

            env = os.environ.copy()
            env["UBSAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1:print_stacktrace=1"
            env["ASAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1"
            try:
                rp = subprocess.run(
                    [bin_path], capture_output=True, text=True,
                    timeout=_RUN_TIMEOUT_S, env=env,
                )
            except subprocess.TimeoutExpired:
                return "RUN ERROR: execution timed out (possible infinite loop)"
            except Exception as exc:
                return f"RUN ERROR: {exc!r}"

            stderr = rp.stderr or ""
            stdout = rp.stdout or ""
            signal_name = None
            if rp.returncode < 0:
                try:
                    import signal as _sig
                    signal_name = _sig.Signals(-rp.returncode).name
                except Exception:
                    signal_name = f"signal_{-rp.returncode}"
            sanitizer_hit = (
                "AddressSanitizer:" in stderr
                or "UndefinedBehaviorSanitizer:" in stderr
                or "runtime error:" in stderr
            )
            crashed = bool(signal_name) or sanitizer_hit or rp.returncode > 128
            if crashed:
                fault = signal_name or (
                    "sanitizer" if sanitizer_hit else f"exit {rp.returncode}"
                )
                tail = stderr.strip()[-1500:]
                return f"OK: reproduced {fault}\n{tail}"
            tail = (stderr or stdout).strip()[-800:]
            return (
                f"RAN OK (exit {rp.returncode}) - no crash. The program "
                f"compiled and ran cleanly, so it did NOT reproduce the "
                f"fault. Output tail:\n{tail}"
            )
        except Exception as exc:  # pragma: no cover - last-resort guard
            return f"RUN ERROR: {exc!r}"
        finally:
            for p in (src_path, bin_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    @staticmethod
    def _is_stub_libc_dir(d: str) -> bool:
        try:
            return any(
                os.path.exists(os.path.join(d, h))
                for h in ("signal.h", "stdio.h", "stdlib.h")
            )
        except Exception:
            return False

    def _make_compile_and_run_tool(self):
        """Return (ToolDef, handler) for compile_and_run_reproducer."""
        from bmc_agent.llm import ToolDef

        tool = ToolDef(
            name="compile_and_run_reproducer",
            description=(
                "Compile the given C reproducer source with the project's "
                "compiler (ASan + UBSan) and run it. Returns "
                "'OK: reproduced <signal/sanitizer>' when it crashes with a "
                "fault, a 'COMPILE ERROR:' block with the gcc error text, a "
                "'RAN OK ... no crash' note when it built but did not "
                "reproduce, or a 'RUN ERROR:' note on timeout. Call this "
                "after each edit and fix the reported error."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "the full C reproducer source to compile and run",
                    },
                },
                "required": ["source"],
            },
        )

        def handler(args: dict) -> object:
            source = str((args or {}).get("source") or "")
            if not source.strip():
                return {"error": "missing 'source' argument"}
            try:
                return {"result": self._compile_and_run_source(source)}
            except Exception as exc:  # pragma: no cover - defensive
                return {"error": f"compile_and_run failed: {exc!r}"}

        return tool, handler

    # ------------------------------------------------------------------
    # _call_llm - tool loop
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str) -> tuple[str, Optional[str]]:
        # Under --agentic, run on the Claude Code agent instead of bmc's
        # in-process tool loop.
        if self._agent_runs_on_claude_code():
            return super()._call_llm(prompt)
        from bmc_agent.llm import LLMError
        from bmc_agent.spec_gen_tools import SpecToolContext, build_spec_gen_tools

        try:
            ctx = SpecToolContext(
                parsed=self.parsed_file,
                corpus_paths=self.corpus_paths,
                all_specs_so_far=self.all_specs,
                boundary_detector=None,
            )
            tools, handlers = build_spec_gen_tools(ctx)
            cr_tool, cr_handler = self._make_compile_and_run_tool()
            tools = list(tools) + [cr_tool]
            handlers = dict(handlers)
            handlers[cr_tool.name] = cr_handler
        except Exception as exc:
            # Any setup error -> treat as unreproducible, never raise.
            return UNREPRODUCIBLE_SENTINEL, None

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
        except Exception as exc:
            return "", f"unexpected: {exc!r}"
        self._last_tool_use_result = result
        if result.error:
            # A budget/cap termination is not fatal - the final text may
            # still carry a reproducer or the sentinel; let parse() decide.
            return result.text or "", None
        return result.text or "", None

    # ------------------------------------------------------------------
    # parse
    # ------------------------------------------------------------------

    def parse(self, response: str) -> Optional[str]:
        text = (response or "").strip()
        if not text:
            return None  # empty response -> BaseAgent reports an error

        # Explicit UNREPRODUCIBLE marker anywhere as the leading content.
        stripped = text.lstrip()
        if stripped.startswith("// UNREPRODUCIBLE"):
            # Honour verbatim (first line only) per the marker convention.
            return stripped.splitlines()[0].strip()

        source = self._extract_c_source(text)
        if source is None:
            # No fenced block / no #include reproducer in the final text.
            return UNREPRODUCIBLE_SENTINEL

        # The model may have emitted the marker outside a fence.
        if source.lstrip().startswith("// UNREPRODUCIBLE"):
            return source.lstrip().splitlines()[0].strip()

        # PUBLIC-API guard: reject a reproducer that does not go through a
        # real project public header (i.e. only calls internal helpers /
        # re-implements project code). Such a "crash" proves nothing.
        if not self._uses_public_api(source):
            logger.info(
                "ReproducerAgent: candidate for '%s' does not use the public "
                "API (no project public header include); treating as "
                "UNREPRODUCIBLE.", self.name,
            )
            return UNREPRODUCIBLE_SENTINEL

        return source

    @staticmethod
    def _extract_c_source(text: str) -> Optional[str]:
        """Pull a C reproducer out of the final message. Prefers a fenced
        ```c block; falls back to any fenced block; falls back to the raw
        text if it looks like C (starts with #include). Returns None when
        nothing source-like is present."""
        import re

        # Fenced ```c (or ```C) block.
        m = re.search(r"```[cC]\b[^\n]*\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # Any fenced block.
        m = re.search(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
        if m:
            inner = m.group(1).strip()
            if inner:
                return inner
        # Bare source.
        if text.lstrip().startswith("#include"):
            return text.strip()
        return None
