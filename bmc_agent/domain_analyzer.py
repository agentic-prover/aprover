"""
Automatic domain knowledge extraction (Pass 1.5).

Analyzes the codebase structure — header files, type declarations, function
signatures — and asks the LLM to produce a concise domain knowledge summary
that is injected into all Phase 1 spec-generation prompts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import ParsedCFile

logger = None  # set lazily to avoid circular imports


def _get_logger():
    global logger
    if logger is None:
        from bmc_agent.logger import get_logger
        logger = get_logger("domain_analyzer")
    return logger


_SYSTEM_PROMPT = """\
You are a senior systems programmer analyzing a C codebase to extract domain knowledge
that will help a formal verification tool generate accurate function specifications.

Your output will be injected verbatim into every spec-generation prompt, so be precise,
concrete, and concise. Focus on invariants and constraints the verifier needs to know —
not on how functions are implemented."""

_USER_PROMPT_TEMPLATE = """\
Analyze the following C codebase structure and produce a domain knowledge summary.

## File names
{file_names}

## Header files (types, macros, constants)
{header_content}

## Key type declarations from source files (structs, typedefs, #defines — no function bodies)
{type_decls}

## Function signatures (all files)
{signatures}

Produce a domain knowledge summary of 3–6 bullet points covering:
- What kind of system this is (OS kernel, embedded driver, etc.) and its target hardware
- Key data structures and the invariants that always hold on their fields
- Hardware abstractions (MMIO regions, DMA buffers, interrupt lines, etc.) and their access constraints
- Memory layout assumptions or address-space conventions
- Any important global state or initialization ordering

Be specific: name the actual structs, constants, and address ranges from the code above.
Do not pad with generic advice. If a point does not apply, omit it.
"""

# Rough character budget for each section — keeps prompt under ~6k tokens total.
_HEADER_BUDGET = 12_000
_TYPE_DECL_BUDGET = 8_000
_SIG_BUDGET = 6_000


def _collect_headers(source_dir: Path, include_dirs: list[str]) -> str:
    """Concatenate all .h files reachable from source_dir (and include_dirs)."""
    search_dirs = [source_dir] + [Path(d) for d in include_dirs]
    parts: list[str] = []
    total = 0
    for d in search_dirs:
        for h in sorted(d.rglob("*.h")):
            try:
                text = h.read_text(encoding="utf-8", errors="replace")
                # Strip C comments and blank lines for compactness
                text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
                text = re.sub(r"//[^\n]*", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if total + len(text) > _HEADER_BUDGET:
                    text = text[: _HEADER_BUDGET - total] + "\n...(truncated)"
                parts.append(f"// {h.name}\n{text}")
                total += len(text)
                if total >= _HEADER_BUDGET:
                    break
            except OSError:
                pass
        if total >= _HEADER_BUDGET:
            break
    return "\n\n".join(parts) or "(none found)"


def _collect_type_decls(file_parsed_c: dict[str, "ParsedCFile"], file_expanded: dict[str, str]) -> str:
    """Extract type declarations (no function bodies) from each parsed file."""
    from bmc_agent.harness_generator import _extract_type_declarations  # local import to avoid cycles

    parts: list[str] = []
    total = 0
    for stem, parsed in sorted(file_parsed_c.items()):
        src = file_expanded.get(stem) or parsed.preprocessed_source or ""
        if not src:
            continue
        decls = _extract_type_declarations(src, parsed).strip()
        if not decls:
            continue
        chunk = f"// {stem}.c\n{decls}"
        if total + len(chunk) > _TYPE_DECL_BUDGET:
            chunk = chunk[: _TYPE_DECL_BUDGET - total] + "\n...(truncated)"
        parts.append(chunk)
        total += len(chunk)
        if total >= _TYPE_DECL_BUDGET:
            break
    return "\n\n".join(parts) or "(none)"


def _collect_signatures(file_parsed_c: dict[str, "ParsedCFile"]) -> str:
    """Collect all function signatures across the codebase."""
    lines: list[str] = []
    for stem, parsed in sorted(file_parsed_c.items()):
        sigs = [f"  {sig.return_type} {name}({', '.join(t for t, _ in sig.parameters)})"
                for name, sig in parsed.functions.items()]
        if sigs:
            lines.append(f"// {stem}.c")
            lines.extend(sigs[:30])  # cap per-file to avoid bloat
            if len(parsed.functions) > 30:
                lines.append(f"  ... +{len(parsed.functions) - 30} more")
    text = "\n".join(lines)
    return text[:_SIG_BUDGET] + ("\n...(truncated)" if len(text) > _SIG_BUDGET else "")


def analyze_codebase(
    source_dir: Path,
    include_dirs: list[str],
    file_parsed_c: dict[str, "ParsedCFile"],
    file_expanded: dict[str, str],
    llm: "LLMClient",
    user_domain_knowledge: str = "",
) -> str:
    """
    Run the LLM domain analysis pass and return a domain knowledge string.

    If *user_domain_knowledge* is non-empty it is appended to the LLM output
    so that user-supplied knowledge always takes precedence.
    """
    log = _get_logger()
    log.info("Pass 1.5: auto-analyzing codebase domain knowledge")

    file_names = ", ".join(f"{s}.c" for s in sorted(file_parsed_c)) or "(none)"
    header_content = _collect_headers(source_dir, include_dirs)
    type_decls = _collect_type_decls(file_parsed_c, file_expanded)
    signatures = _collect_signatures(file_parsed_c)

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        file_names=file_names,
        header_content=header_content,
        type_decls=type_decls,
        signatures=signatures,
    )

    from bmc_agent.llm import agentic_system_prompt
    try:
        result = llm.complete(
            system_prompt=agentic_system_prompt(llm.config, "spec_gen", _SYSTEM_PROMPT),
            user_prompt=user_prompt,
            max_tokens=1024,
            temperature=0.2,
            role="spec_gen",
        )
    except Exception as exc:
        log.warning("Domain knowledge analysis failed (%s) — proceeding without", exc)
        result = ""

    if result:
        log.info("Pass 1.5 complete: domain knowledge extracted (%d chars)", len(result))
        log.debug("Domain knowledge:\n%s", result)

    if user_domain_knowledge and result:
        return result.strip() + "\n\n## User-supplied additional context\n" + user_domain_knowledge.strip()
    return (result or user_domain_knowledge).strip()


# --- Attacker-surface auto-derivation (Pass 1.5, agentic only) --------------

_ATTACK_SURFACE_SYSTEM_PROMPT = """\
You are a security analyst mapping the ATTACKER SURFACE of a C codebase for a
formal verification tool. Your output shapes how the verifier treats inputs.

CRITICAL RULES — your note must be additive-to-safety, never bug-masking:
- Describe ONLY what IS attacker-controlled: the entry points where untrusted
  data enters, and which parameters / struct fields / globals carry that data
  (and how it reaches the rest of the code).
- DO NOT assert that anything is "trusted", "safe", "always valid", or
  "bounded". Listing something as trusted could mask a real bug, so never do it.
- Anything you do not mention is assumed attacker-controlled by default, which
  is the safe assumption — so when unsure, leave it out rather than vouch for it.
- Be concrete: name the real entry functions, fields, and data-flow you SAW in
  the code. Do not invent. If you cannot identify any external input surface,
  say so in one line."""

_ATTACK_SURFACE_USER_TEMPLATE = """\
Map the attacker surface of this C codebase.

## File names
{file_names}

## Header files (types, macros, constants)
{header_content}

## Key type declarations (structs, typedefs, #defines — no function bodies)
{type_decls}

## Function signatures (all files)
{signatures}

Produce a short markdown note titled "## Attacker surface (auto-derived)" with:
- Entry points where untrusted data enters (fuzz harness, syscall/trap handlers,
  loaders of images/ELF/DTB, network/MMIO reads — whichever actually exist here).
- Which parameters / fields / globals carry attacker-controlled bytes, including
  values used as sizes, counts, offsets, or indices.
- The reach: how that data flows toward the functions that will be verified.

Remember: list ONLY attacker-controlled inputs. Never claim anything is trusted
or bounded. Keep it under ~12 bullet points.
"""


def derive_attacker_surface(
    source_dir: Path,
    include_dirs: list[str],
    file_parsed_c: dict[str, "ParsedCFile"],
    file_expanded: dict[str, str],
    llm: "LLMClient",
) -> str:
    """Auto-derive an ATTACKER-SURFACE-ONLY trust-boundary note from the code.

    Returns a markdown note listing only what is attacker-controlled (entry
    points + tainted fields + reach), asserting NOTHING as trusted — so feeding
    it into ``config.threat_model_context`` can only sharpen the conservative
    default, never mask a bug. Returns "" on failure. Intended for --agentic
    runs where the model reads the real code; the prompt forbids fabrication.
    """
    log = _get_logger()
    log.info("Pass 1.5: auto-deriving attacker surface (trust-boundary context)")

    file_names = ", ".join(f"{s}.c" for s in sorted(file_parsed_c)) or "(none)"
    header_content = _collect_headers(source_dir, include_dirs)
    type_decls = _collect_type_decls(file_parsed_c, file_expanded)
    signatures = _collect_signatures(file_parsed_c)

    user_prompt = _ATTACK_SURFACE_USER_TEMPLATE.format(
        file_names=file_names,
        header_content=header_content,
        type_decls=type_decls,
        signatures=signatures,
    )

    from bmc_agent.llm import agentic_system_prompt
    try:
        result = llm.complete(
            system_prompt=agentic_system_prompt(
                llm.config, "spec_gen", _ATTACK_SURFACE_SYSTEM_PROMPT,
            ),
            user_prompt=user_prompt,
            max_tokens=1024,
            temperature=0.2,
            role="spec_gen",
        )
    except Exception as exc:
        log.warning("Attacker-surface derivation failed (%s) — proceeding without", exc)
        return ""

    result = (result or "").strip()
    if result:
        log.info("Pass 1.5: attacker surface derived (%d chars)", len(result))
        log.debug("Attacker surface:\n%s", result)
    return result
