"""
Iterative reproducer generation for dynamic validation.

The single-shot prompt in ``scenario_reproducer.py`` produces compilable C
but the reproducers rarely actually crash under ASan/UBSan — the LLM
hand-waves the input shape ("submit a malformed ACL string") without
constructing one that triggers the precise condition.

This module wraps reproducer generation in a feedback loop:
  1. Attempt 1: rich prompt with explicit "crash signal" guidance + freedom
     to use private/internal APIs when the public API can't reach the bug
  2. Compile + run with ASan/UBSan
  3. If no crash: re-prompt with the program output + ask for a stronger
     version that DOES crash
  4. Up to MAX_ATTEMPTS iterations

Returns the strongest result (crash > no_crash > compile_error > unreproducible).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal as _sig
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo, ParsedCFile

logger = logging.getLogger("reproducer_loop")

MAX_ATTEMPTS = 3
DYN_TIMEOUT_S = 30
COMPILE_TIMEOUT_S = 60


_INITIAL_PROMPT = """\
You are a security auditor writing a CRASH REPRODUCER in C.

A formal verifier (CBMC) flagged a counterexample in `{fn_name}` and a
second auditor judged it a real bug:

---
{attacker_scenario}
---

Your job: write a SELF-CONTAINED C program with `main()` that, when
compiled and run, ACTUALLY CRASHES under AddressSanitizer +
UndefinedBehaviorSanitizer. The crash IS the evidence — no crash means
the report is unconfirmed.

# What "actually crashes" means
The program is built with:
  gcc -O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer \\
      -I{header_dir} -L{lib_dir} -Wl,-rpath,{lib_dir} -larchive

A successful reproducer either:
  - Receives SIGSEGV / SIGABRT / SIGBUS (segfault / abort / signal kill)
  - Triggers "AddressSanitizer:" or "UndefinedBehaviorSanitizer:" in
    stderr (e.g. heap-buffer-overflow, use-after-free,
    null-pointer-dereference, signed-integer-overflow,
    runtime-error: nullptr-with-offset)
  - Exits with a non-zero signal-coded status

A program that runs to completion and exits 0 has FAILED to confirm.

# Constraints
- Use libarchive headers: `#include <archive.h>` and `#include <archive_entry.h>`.
  Both are at `{header_dir}`.
- PREFER the public API. But if the bug requires internal state that the
  public API can't reach, you MAY use any function declared in archive.h
  or archive_entry.h that exists (e.g., archive_entry_acl_add_entry).
- Construct EXACT bytes. If the scenario mentions "malformed ACL string",
  write out the literal string that triggers the bug (e.g.
  `const char *acl_text = "user:r--";`).
- For ACL bugs: `archive_entry_acl_from_text(entry, acl_text, type)` is
  the most direct path into the parser. Type values:
    ARCHIVE_ENTRY_ACL_TYPE_ACCESS, ARCHIVE_ENTRY_ACL_TYPE_DEFAULT,
    ARCHIVE_ENTRY_ACL_TYPE_ALLOW, ARCHIVE_ENTRY_ACL_TYPE_AUDIT,
    ARCHIVE_ENTRY_ACL_TYPE_ALARM.
- To serialize ACLs (text_len / to_text bugs): call
  `archive_entry_acl_to_text(entry, &len, flags)` AFTER injecting
  malicious entries via `archive_entry_acl_add_entry(entry, ...)`.
- Try LARGE/EXTREME values where the bug class invites it (many entries,
  long strings, INT_MAX-adjacent counters).
- If after careful thought you genuinely cannot construct one (e.g. the
  scenario assumes a dangling pointer that no public/private API
  produces), output EXACTLY `// UNREPRODUCIBLE: <one-line reason>`.

Function source for reference:

```c
{fn_body}
```

Output ONLY the C program (or the UNREPRODUCIBLE line). No prose,
no markdown fences. First line MUST be `#include` or `// UNREPRODUCIBLE:`.
"""


_RETRY_PROMPT = """\
Your previous reproducer for `{fn_name}` compiled but did NOT crash under
AddressSanitizer + UndefinedBehaviorSanitizer. We need a stronger
reproducer.

# Last attempt's outcome
exit_code: {exit_code}
stdout (truncated):
{stdout}
stderr (truncated):
{stderr}

# Last attempt's source (the C you produced last time)
```c
{prev_source}
```

# What to fix
A successful reproducer MUST crash with SIGSEGV/SIGABRT/SIGBUS or trigger
an AddressSanitizer/UBSan report. The previous program ran to completion,
which means the input you constructed did NOT exercise the bug path.

Re-read the attacker scenario:
---
{attacker_scenario}
---

Common reasons attempt {attempt_num} fails to crash:
  - Input is well-formed enough that the library validates it before
    reaching the suspect code path. Make the input MORE malformed.
  - The library swallows the malformed input and returns an error code
    instead of dereferencing it. Try a different entry point that does
    less validation, or escalate to one of the internal-but-exported
    helpers (e.g. archive_entry_acl_add_entry to inject ACL state
    bypassing the text parser).
  - Off-by-one is too small to trip ASan. Make the violation BIGGER —
    e.g. iterate to push the malloc'd buffer past page boundaries, or
    pass length values near INT_MAX, or construct ACLs with thousands
    of entries.
  - The bug needs a specific sequence of API calls to set internal
    state first. Re-read the function body and reproduce that sequence.

Constraints unchanged: libarchive headers at `{header_dir}`; first line
must be `#include` (or `// UNREPRODUCIBLE: <reason>` if you've concluded
this scenario cannot crash). No prose, no markdown fences.

Output the NEW C program.
"""


# Outcome strength: higher number = stronger evidence of a real bug
_OUTCOME_RANK = {
    "confirmed_dynamic": 4,
    "timeout": 3,
    "not_triggered": 2,
    "compile_error": 1,
    "unreproducible": 0,
    "llm_no_reproducer": 0,
    "skipped": 0,
}


def _rank(outcome: str) -> int:
    return _OUTCOME_RANK.get(outcome, 0)


def _llm_generate(
    llm: "LLMClient",
    prompt: str,
    fn_name: str,
    max_tokens: int = 4096,
) -> Optional[str]:
    """Single LLM call → C source. Returns None on UNREPRODUCIBLE or bad output."""
    from bmc_agent.llm import agentic_system_prompt
    try:
        raw = llm.complete(
            agentic_system_prompt(
                llm.config, "realism",
                "You are a security-audit helper that produces compilable C reproducers.",
            ),
            prompt,
            max_tokens=max_tokens,
            thinking=False,
            role="realism",
        )
    except Exception as exc:
        logger.warning(
            "reproducer_loop LLM call failed for '%s': %s", fn_name, exc
        )
        return None

    text = (raw or "").strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    if text.startswith("// UNREPRODUCIBLE"):
        logger.info(
            "reproducer_loop: LLM declined for '%s' (%s)", fn_name, text[:120]
        )
        return None
    if not text.startswith("#include"):
        logger.info(
            "reproducer_loop: LLM output for '%s' doesn't start with #include "
            "(first 120 chars: %r)",
            fn_name, text[:120],
        )
        return None
    return text


def _compile_and_run(
    c_source: str,
    *,
    fn_name: str,
    libarchive_build: str,
    libarchive_inc: str,
    out_dir: Path,
    attempt: int,
) -> dict:
    """Compile c_source with ASan/UBSan, run binary, return outcome dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src_path = out_dir / f"reproducer_attempt{attempt}.c"
    src_path.write_text(c_source)
    binary_path = out_dir / f"reproducer_attempt{attempt}.bin"

    compile_cmd = [
        "gcc", "-O0", "-g",
        "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer",
        f"-I{libarchive_inc}",
        f"-I{Path(libarchive_build).parent}",
        str(src_path),
        f"-L{libarchive_build}",
        f"-Wl,-rpath,{libarchive_build}",
        "-larchive",
        "-o", str(binary_path),
    ]
    try:
        cp = subprocess.run(
            compile_cmd, capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "outcome": "compile_error",
            "reason": f"gcc timed out (>{COMPILE_TIMEOUT_S}s)",
            "harness_path": str(src_path),
            "attempt": attempt,
            "exit_code": None, "stdout": "", "stderr": "",
        }
    if cp.returncode != 0:
        return {
            "outcome": "compile_error",
            "compile_stderr": cp.stderr[:1500],
            "harness_path": str(src_path),
            "attempt": attempt,
            "exit_code": None, "stdout": "", "stderr": cp.stderr[:1500],
        }

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = libarchive_build + ":" + env.get("LD_LIBRARY_PATH", "")
    env["UBSAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1:print_stacktrace=1"
    env["ASAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1"
    try:
        rp = subprocess.run(
            [str(binary_path)],
            capture_output=True, text=True,
            timeout=DYN_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "outcome": "timeout",
            "harness_path": str(src_path),
            "binary_path": str(binary_path),
            "attempt": attempt,
            "exit_code": None, "stdout": "", "stderr": "",
        }

    stderr = rp.stderr or ""
    stdout = rp.stdout or ""
    signal_name = None
    if rp.returncode < 0:
        try:
            signal_name = _sig.Signals(-rp.returncode).name
        except Exception:
            signal_name = f"signal_{-rp.returncode}"
    sanitizer_hit = (
        "AddressSanitizer:" in stderr
        or "UndefinedBehaviorSanitizer:" in stderr
        or "runtime error:" in stderr
    )
    crashed = bool(signal_name) or sanitizer_hit or rp.returncode > 128
    return {
        "outcome": "confirmed_dynamic" if crashed else "not_triggered",
        "exit_code": rp.returncode,
        "signal_name": signal_name,
        "sanitizer_hit": sanitizer_hit,
        "stderr": stderr[:3000],
        "stdout": stdout[:1000],
        "stderr_excerpt": stderr[:2000],
        "stdout_excerpt": stdout[:500],
        "harness_path": str(src_path),
        "binary_path": str(binary_path),
        "attempt": attempt,
    }


def run_reproducer_loop(
    *,
    func: "FunctionInfo",
    attacker_scenario: str,
    parsed_file: "ParsedCFile",
    llm: "LLMClient",
    out_dir: Path,
    libarchive_build: str,
    libarchive_inc: str,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict:
    """Iteratively generate, compile, and run an ASan/UBSan reproducer for
    a realism-realistic finding. Returns the strongest outcome across
    up to ``max_attempts`` attempts plus a per-attempt history.

    Stops early on first ``confirmed_dynamic`` outcome.
    """
    if not attacker_scenario or not attacker_scenario.strip():
        return {"outcome": "skipped", "reason": "no attacker_scenario", "attempts": []}
    if not Path(libarchive_build).is_dir() or not any(
        Path(libarchive_build).glob("libarchive.so*")
    ):
        return {
            "outcome": "skipped",
            "reason": f"libarchive build not found at {libarchive_build}",
            "attempts": [],
        }
    if not shutil.which("gcc"):
        return {"outcome": "skipped", "reason": "gcc not on PATH", "attempts": []}

    fn_name = func.name
    fn_body = (func.body or "(body unavailable)")[:6000]

    attempts_log: list[dict] = []
    best: Optional[dict] = None
    prev_source = ""
    last_run: Optional[dict] = None

    for i in range(1, max_attempts + 1):
        if i == 1:
            prompt = _INITIAL_PROMPT.format(
                fn_name=fn_name,
                attacker_scenario=attacker_scenario.strip(),
                fn_body=fn_body,
                header_dir=libarchive_inc,
                lib_dir=libarchive_build,
            )
        else:
            # Skip retry when there's nothing to learn from (LLM refusal /
            # compile error with no stderr to feed back).
            if last_run is None or not prev_source:
                break
            prompt = _RETRY_PROMPT.format(
                fn_name=fn_name,
                attempt_num=i,
                attacker_scenario=attacker_scenario.strip(),
                prev_source=prev_source[:8000],
                exit_code=last_run.get("exit_code"),
                stdout=(last_run.get("stdout") or "")[:1500],
                stderr=(last_run.get("stderr") or "")[:2000],
                header_dir=libarchive_inc,
                lib_dir=libarchive_build,
            )

        c_source = _llm_generate(llm, prompt, fn_name)
        if c_source is None:
            attempts_log.append({"attempt": i, "outcome": "llm_no_reproducer"})
            if best is None:
                best = {"outcome": "llm_no_reproducer",
                        "reason": "LLM declined or produced unusable C",
                        "attempt": i}
            break

        result = _compile_and_run(
            c_source,
            fn_name=fn_name,
            libarchive_build=libarchive_build,
            libarchive_inc=libarchive_inc,
            out_dir=out_dir,
            attempt=i,
        )
        attempts_log.append({
            "attempt": i,
            "outcome": result["outcome"],
            "exit_code": result.get("exit_code"),
            "signal_name": result.get("signal_name"),
            "sanitizer_hit": result.get("sanitizer_hit"),
            "harness_path": result.get("harness_path"),
        })

        logger.info(
            "reproducer_loop[%s] attempt %d/%d → %s (signal=%s sanitizer=%s)",
            fn_name, i, max_attempts, result["outcome"],
            result.get("signal_name"), result.get("sanitizer_hit"),
        )

        if best is None or _rank(result["outcome"]) > _rank(best["outcome"]):
            best = result

        if result["outcome"] == "confirmed_dynamic":
            # Crash found — done.
            break

        # Otherwise: feed last result into the retry prompt for attempt i+1.
        prev_source = c_source
        last_run = result

    if best is None:
        best = {"outcome": "skipped", "reason": "no attempts ran"}
    best["attempts"] = attempts_log
    best["n_attempts"] = len(attempts_log)
    return best
