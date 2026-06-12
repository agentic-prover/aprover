"""
Scenario-guided dynamic reproducer generator.

When realism check votes REALISTIC and supplies an ``attacker_scenario``
description (natural-language English), this module asks the LLM to
translate that scenario into a concrete C program that drives the public
API to trigger the violation. The generated program is fed back to
``DynamicValidator`` as a ``system_entry_reproducer`` — if it crashes
at runtime under sanitizers, the finding gets promoted to the
``confirmed_dynamic`` tier.

This is the missing edge between realism's qualitative judgment and a
runtime-verifiable PoC. Without it, every dynamic harness uses the
CBMC-substituted witness verbatim (which often picks extreme values
that don't trigger the bug in practice), so the finding gets stuck at
``confirmed_system_entry`` even when it's a real bug.

Single LLM call per realism-confirmed finding without prior dynamic
crash. Caps at ~4096 output tokens. Fails open (returns None) when the
LLM can't produce compilable C.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import ParsedCFile, FunctionInfo


_SCENARIO_REPRODUCER_PROMPT = """You are helping audit a C library for security bugs.

A formal-verification tool (CBMC) found a counterexample in this function:

  {fn_name}({fn_signature})

A second LLM auditor judged the finding REALISTIC and described the
attacker scenario as:

---
{attacker_scenario}
---

Your job: produce a SELF-CONTAINED C program (one ``main()`` function
plus any helpers) that drives the public API the same way an attacker
would, to trigger the violation at runtime. The program will be
compiled with GCC + AddressSanitizer + UndefinedBehaviorSanitizer
(no special flags needed beyond -fsanitize=address,undefined; you don't
write the compile command). If the violation is real, your program
should crash with SIGSEGV / SIGABRT / SIGFPE.

Constraints:
  * Use ONLY public-API functions from <archive.h> / <archive_entry.h>
    (assume the library is installed; you can #include them).
  * No project-internal headers (no archive_private.h, etc.).
  * No filesystem dependencies — keep everything in-memory if possible
    (archive_read_open_memory, archive_write_open_memory).
  * If the bug needs malformed input bytes, define the bytes inline
    as a C array.
  * Wrap the suspect call in a region marked with `// === BUG TRIGGER ===`
    so a reviewer can navigate to it.
  * If you can't construct a working reproducer (e.g. the scenario
    needs internal state not reachable through the public API), output
    exactly the string `// UNREPRODUCIBLE: <one-line reason>` and
    nothing else.

CRITICAL API SIGNATURES — do NOT invent variants of these. Wrong usage
crashes the reproducer itself (stack corruption / SEGV in the I/O
plumbing) and produces a false-positive sanitizer hit that is NOT the
bug you're trying to demonstrate:

  // Reading from an in-memory buffer:
  //   buff:        const pointer to the input bytes
  //   size:        VALUE (size_t), not a pointer
  int archive_read_open_memory(struct archive *, const void *buff, size_t size);

  // Writing to an in-memory buffer:
  //   buffer:      caller-allocated writable buffer (void*, not void**)
  //   buffSize:    VALUE (size_t), the buffer's capacity — NOT a pointer
  //   used:        size_t* — out-parameter, must point to its OWN size_t
  //                (NEVER alias it with anything else, and never reuse the
  //                 same address as the buffSize argument).
  int archive_write_open_memory(struct archive *, void *buffer,
                                size_t buffSize, size_t *used);

  // CORRECT call:
  //     char buf[4096];
  //     size_t used = 0;
  //     archive_write_open_memory(a, buf, sizeof(buf), &used);
  //
  // WRONG (do NOT do any of these):
  //     archive_write_open_memory(a, &buf, &cap, &cap);     // aliasing used with buffSize, &cap is wrong type
  //     archive_write_open_memory(a, buf, &cap, &used);     // buffSize must be a value
  //     archive_write_open_memory(a, &buf, cap, &used);     // buffer is void*, not void**

MEMORY MANAGEMENT — every allocator-returning public-API call has a
matching free. If you skip these, LeakSanitizer fires and the reviewer
cannot tell whether the crash you produced is the bug or just leak
noise:

  * archive_read_new()  ↔  archive_read_free(a)
  * archive_write_new() ↔  archive_write_free(a)
  * archive_match_new() ↔  archive_match_free(m)
  * archive_entry_new() ↔  archive_entry_free(e)
  * char *t = archive_entry_acl_to_text(...);   // malloc'd
        ...
        free(t);                                 // MUST free

Function source for reference:

```c
{fn_body}
```

Output ONLY the C program (or the UNREPRODUCIBLE line). No prose,
no markdown fences, no explanation. The first line must be either
``#include`` or ``// UNREPRODUCIBLE:``.
"""


# Generic (non-libarchive) reproducer prompt. The libarchive prompt above is
# hardcoded to that library's public API (archive.h, archive_*_open_memory, ACL
# helpers); on any OTHER target the LLM dutifully tries to use libarchive and
# emits garbage / "not a libarchive symbol". For internal code (a kernel VFS fn,
# a packet handler, ...) the reproducer must CALL THE FUNCTION DIRECTLY — there
# is no public-API wrapper. Same placeholders so .format() is interchangeable.
_GENERIC_SCENARIO_REPRODUCER_PROMPT = """You are helping audit a C program for security bugs.

A formal-verification tool (CBMC) found a counterexample in this function:

  {fn_name}({fn_signature})

A second LLM auditor judged the finding REALISTIC and described the
attacker scenario as:

---
{attacker_scenario}
---

Your job: produce a SELF-CONTAINED C program (one ``main()`` plus any
helpers) that calls ``{fn_name}`` the way the attacker scenario describes,
to trigger the violation at runtime. It is compiled with GCC + AddressSanitizer
+ UndefinedBehaviorSanitizer. If the violation is real it should crash
(SIGSEGV / SIGABRT / SIGFPE) or trip an ASan/UBSan report.

Constraints:
  * This is INTERNAL code — there is NO public-API wrapper. Call ``{fn_name}``
    DIRECTLY. The function, its parameter types, and the project's own
    declarations are compiled together with your program (the build includes
    the source under test); do NOT redefine them. If a small helper type is
    needed to construct an argument, declare it minimally.
  * Do NOT assume any specific third-party library (no ``<archive.h>`` etc.).
    Use only the standard headers you actually need (``<stdint.h>``,
    ``<string.h>``, ``<stdlib.h>`` ...).
  * Construct the EXACT argument values the scenario calls for — a crafted byte
    buffer, an out-of-range length / index / offset, a malformed string.
    Define any input bytes inline as a C array, and match {fn_name}'s parameter
    order and types precisely.
  * Wrap the suspect call in a region marked ``// === BUG TRIGGER ===``.
  * Free anything you allocate so LeakSanitizer noise doesn't mask the bug.
  * If you genuinely cannot construct one (the scenario needs state no caller
    can produce), output exactly ``// UNREPRODUCIBLE: <one-line reason>``.

Function source for reference:

```c
{fn_body}
```

Output ONLY the C program (or the UNREPRODUCIBLE line). No prose, no markdown
fences. The first line must be ``#include`` or ``// UNREPRODUCIBLE:``.
"""


def _is_libarchive_target(parsed_file) -> bool:
    """True when the target really is libarchive — only then is the libarchive-
    specific reproducer prompt appropriate. Detected from the source path (the
    libarchive eval runs on ``.../libarchive/...`` paths) or an ``archive.h``
    include. Everything else gets the generic 'call the function directly' prompt.
    """
    path = (getattr(parsed_file, "path", "") or "")
    primary = (getattr(parsed_file, "primary_source", "") or "")
    if "libarchive" in (path + " " + primary).lower():
        return True
    pre = getattr(parsed_file, "preprocessed_source", "") or ""
    return ("archive.h" in pre) or ("archive_entry.h" in pre)


def generate_reproducer(
    func: "FunctionInfo",
    attacker_scenario: str,
    parsed_file: "ParsedCFile",
    llm: "LLMClient",
) -> Optional[str]:
    """Generate a C reproducer from a natural-language attacker scenario.

    Returns the C source string on success, or ``None`` when the LLM
    refuses (``// UNREPRODUCIBLE`` marker), can't produce compilable
    output, or the call fails. The caller passes the returned string
    to ``DynamicValidator.validate(system_entry_reproducer=...)``.
    """
    if not attacker_scenario or not attacker_scenario.strip():
        return None

    fn_name = func.name
    fn_signature = _format_signature(func)
    fn_body = (func.body or "(body unavailable)")[:6000]

    _tmpl = (
        _SCENARIO_REPRODUCER_PROMPT if _is_libarchive_target(parsed_file)
        else _GENERIC_SCENARIO_REPRODUCER_PROMPT
    )
    prompt = _tmpl.format(
        fn_name=fn_name,
        fn_signature=fn_signature,
        attacker_scenario=attacker_scenario.strip(),
        fn_body=fn_body,
    )

    from bmc_agent.llm import agentic_system_prompt
    try:
        raw = llm.complete(
            agentic_system_prompt(
                llm.config, "realism",
                "You are a security-audit helper that produces compilable C reproducers.",
            ),
            prompt,
            max_tokens=4096,
            thinking=False,
            role="realism",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Scenario-reproducer LLM call failed for '%s': %s", fn_name, exc
        )
        return None

    text = (raw or "").strip()
    # Strip code fences if the LLM emitted them despite instructions.
    if text.startswith("```"):
        # Trim until first newline after the opening fence.
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    if text.startswith("// UNREPRODUCIBLE"):
        logger.info(
            "Scenario reproducer: LLM declined to construct one for '%s' (%s)",
            fn_name, text[:120],
        )
        return None

    if not text.startswith("#include"):
        logger.info(
            "Scenario reproducer: LLM output for '%s' doesn't start with #include; "
            "discarding (first 120 chars: %r)",
            fn_name, text[:120],
        )
        return None

    logger.info(
        "Scenario reproducer: generated %d-char C reproducer for '%s'",
        len(text), fn_name,
    )
    return text


def _format_signature(func: "FunctionInfo") -> str:
    """Best-effort signature for the prompt. Falls back to function name
    when richer info isn't available on the FunctionInfo."""
    sig = getattr(func, "signature", None)
    if sig:
        # FunctionSignature has .params (list) and .return_type
        try:
            ret = getattr(sig, "return_type", None) or "void"
            params = getattr(sig, "params", None) or []
            params_str = ", ".join(str(p) for p in params) or "void"
            return f"{ret} {func.name}({params_str})"
        except Exception:
            pass
    return func.name + "(...)"
