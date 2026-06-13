"""Phase 1 (realism-enforcement plan): harness-refinement outcome **C**.

A unit-level dynamic harness links *undefined* external globals to address 0
(`-Wl,--unresolved-symbols=ignore-all`). For a boot-init-trusted global defined
in a SIBLING translation unit — e.g. ``uint32_t *fb_base = NULL;`` in ``fb.c``,
set by ``fb_init()`` at boot and only declared ``extern`` in ``irq.c`` — that
default-to-NULL is a HARNESS ARTIFACT: at runtime the init always runs before
any consumer, so the global is never NULL. A ``confirmed_dynamic`` SIGSEGV that
is really just this NULL deref is a false positive.

This module gives an UNREALISTIC-leaning ``confirmed_dynamic`` finding an
EMPIRICAL refinement step instead of only an LLM judgment:

  1. find the undefined externs the harness left unresolved;
  2. classify which are *boot-init-trusted* (file-scope NULL/0 init in a sibling
     ``.c``, assigned only inside an ``*_init``-style function);
  3. MATERIALIZE them in the harness (pointer ⇒ ``calloc(1, sizeof(*g))``, the
     same conservative model as the CBMC side, commit b4aa03c / 279b486);
  4. re-run the dynamic validator.

Decision (caller applies it):
  * refined harness **no longer crashes** ⇒ the fault was the NULL-default
    artifact ⇒ demote honestly.
  * refined harness **still crashes** ⇒ the fault survives a materialized
    (non-NULL) global ⇒ keep ``confirmed``.

SOUNDNESS — why this can never demote a real bug. The materialization is
``calloc(1, sizeof(*g))``: a single zeroed element, the SMALLEST non-NULL
object. Any access beyond index 0 (a genuine out-of-bounds) still faults on the
1-element buffer, so a real OOB re-crashes and is KEPT. Only a pure
NULL-dereference (index 0 of a never-initialized pointer) is cleaned. The model
is conservative by construction: it can turn a NULL-deref artifact clean, but it
can never enlarge a buffer enough to mask a real overflow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# undefined reference to `fb_base'   (gcc/ld, with optional quoting variants)
_UNDEF_RE = re.compile(r"undefined reference to [`'\"]?([A-Za-z_]\w*)[`'\"]?")


@dataclass
class TrustedGlobal:
    name: str
    ctype: str          # e.g. "uint32_t *"  or  "uint32_t"
    is_pointer: bool
    init_fn: str        # the *_init function that sets it (evidence of boot-init-trusted)


def parse_undefined_externs(compile_err: str | None) -> list[str]:
    """Symbols ld reported as undefined references in the harness link.

    These are the globals the unit harness left to default-to-0/NULL. Order
    preserved, de-duplicated. Filters out obvious function symbols is NOT done
    here (a function ref can't be materialized as a global, but the classifier
    rejects anything without a matching data definition, so it's harmless)."""
    if not compile_err:
        return []
    seen: dict[str, None] = {}
    for m in _UNDEF_RE.finditer(compile_err):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def classify_boot_init_trusted(name: str, sibling_sources: dict[str, str]) -> TrustedGlobal | None:
    """If ``name`` is a boot-init-trusted global defined in one of the sibling
    sources, return its TrustedGlobal; else None.

    Boot-init-trusted (mirrors the CBMC ``init-trusted`` tier, b4aa03c):
      * a file-scope definition with a NULL / 0 initializer, AND
      * (re)assigned only inside a function whose name ends in ``_init`` (or is
        named ``init``) — i.e. set at boot, never by an attacker-reachable path.

    Conservative: if the global is assigned anywhere OUTSIDE an ``*_init``
    function, it is NOT classified trusted (could be attacker-influenced), so we
    don't materialize it and the finding is kept.
    """
    # Definition: optionally `static`, a type, optional `*`, the name, `= NULL|0`.
    defn = re.compile(
        r"^[ \t]*(?:static\s+)?"
        r"([A-Za-z_][\w \t]*?[\w])\s*(\*?)\s*"
        + re.escape(name)
        + r"\s*=\s*(NULL|\(\(void\s*\*\)0\)|0)\s*;",
        re.M,
    )
    for src in sibling_sources.values():
        s = _strip_comments(src)
        m = defn.search(s)
        if not m:
            continue
        base_type = m.group(1).strip()
        is_ptr = m.group(2) == "*"
        # Find every assignment `name =` (not `==`, not the definition's own init
        # which carries a type prefix). Confirm each lives inside an *_init fn.
        init_fn = _assigned_only_in_init(s, name)
        if init_fn is None:
            return None
        ctype = (base_type + " *") if is_ptr else base_type
        return TrustedGlobal(name=name, ctype=ctype, is_pointer=is_ptr, init_fn=init_fn)
    return None


def _assigned_only_in_init(stripped_src: str, name: str) -> str | None:
    """Return the name of the *_init function that assigns ``name`` iff EVERY
    assignment to ``name`` is inside an ``*_init`` (or ``init``) function. Else
    None. Uses a brace-depth scan to attribute each assignment to its enclosing
    top-level function."""
    assign_re = re.compile(r"(?<![=!<>])\b" + re.escape(name) + r"\s*=(?!=)")
    # Map of (start,end,fn_name) for top-level functions.
    fns = _top_level_functions(stripped_src)
    init_fn_seen: str | None = None
    for m in assign_re.finditer(stripped_src):
        pos = m.start()
        owner = None
        for (s, e, fn) in fns:
            if s <= pos < e:
                owner = fn
                break
        if owner is None:
            # File scope: the only legal `name =` here is the definition's own
            # initializer (C has no bare top-level assignment statements). It was
            # already matched as `= NULL/0`, so it carries no taint — skip it.
            continue
        if not re.search(r"(_init|^init)$", owner):
            return None
        init_fn_seen = owner
    return init_fn_seen


def _top_level_functions(stripped_src: str) -> list[tuple[int, int, str]]:
    """List of (body_start, body_end, name) for top-level function definitions.
    Lightweight brace matcher; good enough for kernel .c modules."""
    out: list[tuple[int, int, str]] = []
    hdr = re.compile(r"([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{")
    i = 0
    n = len(stripped_src)
    while i < n:
        m = hdr.search(stripped_src, i)
        if not m:
            break
        name = m.group(1)
        # skip control keywords that look like calls
        if name in ("if", "for", "while", "switch", "return", "sizeof"):
            i = m.end()
            continue
        depth = 1
        j = m.end()
        while j < n and depth:
            c = stripped_src[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        out.append((m.end(), j, name))
        i = j
    return out


def synthesize_materialization(globals_: list[TrustedGlobal]) -> str:
    """C source block that DEFINES the trusted externs and materializes pointers
    to a single zeroed element via a constructor (runs before main()). Scalars
    get a plain zero-init definition so the link resolves without changing their
    value (conservative: no guessed dimensions)."""
    if not globals_:
        return ""
    lines = ["", "/* AMC harness-refinement: materialize boot-init-trusted externs */"]
    body = []
    for g in globals_:
        if g.is_pointer:
            lines.append(f"{g.ctype}{g.name} = (void*)0;")
            body.append(
                f"    if (!{g.name}) {{ {g.name} = calloc(1, sizeof(*{g.name})); }}"
                f"  /* {g.name}: real boot runs {g.init_fn}() first */"
            )
        else:
            lines.append(f"{g.ctype} {g.name};")
    lines.append("#include <stdlib.h>")
    lines.append("__attribute__((constructor)) static void __amc_materialize_trusted(void){")
    lines.extend(body)
    lines.append("}")
    return "\n".join(lines) + "\n"


def inject_materialization(harness_source: str, block: str) -> str:
    """Insert the materialization block after the last top-of-file #include so
    the definitions precede any use. If there is no #include, prepend."""
    if not block:
        return harness_source
    lines = harness_source.splitlines(keepends=True)
    last_inc = -1
    for idx, ln in enumerate(lines):
        if ln.lstrip().startswith("#include"):
            last_inc = idx
    insert_at = last_inc + 1 if last_inc >= 0 else 0
    return "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])


def plan_refinement(
    compile_err_or_harness_log: str | None,
    sibling_sources: dict[str, str],
    referenced_idents: set[str] | None = None,
) -> list[TrustedGlobal]:
    """End-to-end planning: from the link error (or any text listing undefined
    refs), return the boot-init-trusted globals to materialize. ``referenced_idents``
    (if given) further restricts to names the harness actually uses."""
    names = parse_undefined_externs(compile_err_or_harness_log)
    out: list[TrustedGlobal] = []
    for nm in names:
        if referenced_idents is not None and nm not in referenced_idents:
            continue
        tg = classify_boot_init_trusted(nm, sibling_sources)
        if tg is not None:
            out.append(tg)
    return out
