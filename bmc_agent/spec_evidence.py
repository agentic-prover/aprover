"""Evidence gathering for the v2 spec generator.

Three independent evidence sources feed the spec drafter, so its output
is grounded in something other than reading the function body:

  1. Caller harvest — grep the corpus for call sites of the function,
     pick K representative ones, capture ±N lines of context.
  2. Doc annotations — parse Doxygen-style annotations (\\param, \\pre,
     \\post, \\returns) from the comment block preceding the definition.
  3. Universal patterns — wrap universal_contracts.py's signature-pattern
     rules (paired_pointers, length bounds, etc.) and tag them.

The bundle these produce is the LLM prompt's context. Each emitted
clause carries an evidence tag the orchestrator copies into Spec.evidence
so feedback_loop can later drop low-trust clauses preferentially.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bmc_agent.parser import FunctionInfo, ParsedCFile


# ---------- data classes ----------------------------------------------------


@dataclass
class CallerEvidence:
    """One observed call site of the function under spec."""

    file: str
    line: int                       # 1-indexed line number of the call
    enclosing_function: Optional[str]
    context_lines: list[str]        # source lines around the call
    call_line_text: str             # the exact line containing the call

    def render(self) -> str:
        """Compact LLM-context block for one caller."""
        head = f"// {self.file}:{self.line}"
        if self.enclosing_function:
            head += f"  (inside {self.enclosing_function})"
        body = "\n".join(self.context_lines)
        return f"{head}\n{body}"


@dataclass
class DocClause:
    """A single Doxygen-style annotation parsed from the leading comment."""

    raw_text: str
    annotation_type: str            # "param" | "pre" | "post" | "returns" | "brief"
    param_name: Optional[str] = None

    def render(self) -> str:
        return f"{self.annotation_type}: {self.raw_text}"


@dataclass
class SeedClause:
    """A clause derived deterministically from the signature pattern."""

    clause: str                     # e.g. "start <= end"
    pattern_name: str               # e.g. "paired_pointers"

    def render(self) -> str:
        return f"{self.clause}  /* {self.pattern_name} */"


@dataclass
class EvidenceBundle:
    """All evidence gathered for one function, ready to feed the LLM."""

    fn_name: str
    callers: list[CallerEvidence] = field(default_factory=list)
    address_taken_sites: list[CallerEvidence] = field(default_factory=list)
    doc_annotations: list[DocClause] = field(default_factory=list)
    seed_clauses: list[SeedClause] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.callers
            or self.address_taken_sites
            or self.doc_annotations
            or self.seed_clauses
        )


# ---------- caller harvest --------------------------------------------------

# A "call site" is a textual match of `fn_name(` that is not:
#   - inside a // line comment
#   - inside a /* ... */ block comment
#   - inside a string literal on that line
#   - a function declaration (line ends in `);` with no body brace)
#   - the function's own definition (line that opens the function body)
#
# This is heuristic, not a real parser. False positives surface as a
# slightly noisier prompt; false negatives reduce evidence. The cost of
# either is small relative to a missed real caller.

_TEST_PATH_HINT_RX = re.compile(r"(^|/)(test_[^/]+|[^/]+_test\.c|[^/]+/test/)")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_HINT_RX.search(path))


def _strip_string_literals(line: str) -> str:
    """Replace "..." and '...' contents with empty quotes so a regex match
    that lands inside a string literal won't accidentally count."""
    out = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"' or c == "'":
            quote = c
            out.append(quote)
            i += 1
            while i < len(line) and line[i] != quote:
                # Skip escaped char.
                if line[i] == "\\" and i + 1 < len(line):
                    i += 2
                    continue
                i += 1
            out.append(quote)
            if i < len(line):
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _line_is_comment_only(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("//") or s.startswith("*") or s.startswith("/*")


def _enclosing_function_for_line(
    source_lines: list[str],
    line_idx: int,
    candidate_fn_names: set[str],
) -> Optional[str]:
    """Best-effort: walk backward from line_idx and find the most recent
    line that looks like a function definition whose name is in
    candidate_fn_names. The candidate set comes from the parsed file.

    Returns the function name, or None if no plausible candidate is found
    within a 200-line lookback (functions longer than that get None and
    we move on).
    """
    fn_def_rx = re.compile(r"\b(\w+)\s*\(")
    lookback = min(200, line_idx + 1)
    brace_balance = 0
    for j in range(line_idx, line_idx - lookback, -1):
        if j < 0:
            break
        ln = source_lines[j]
        brace_balance += ln.count("}") - ln.count("{")
        # When we cross a net-open brace going backward, we've left the
        # function we were in. The next match for a function-signature-
        # looking line on that or an earlier line is the enclosing fn.
        if brace_balance < 0:
            # Look at this and a few following lines for a sig-line.
            for k in range(max(0, j - 3), min(len(source_lines), j + 2)):
                m = fn_def_rx.search(source_lines[k])
                if m and m.group(1) in candidate_fn_names:
                    return m.group(1)
            return None
    return None


def harvest_callers(
    fn_name: str,
    corpus_paths: list[Path],
    *,
    k: int = 5,
    context_radius: int = 8,
    candidate_fn_names: Optional[set[str]] = None,
) -> list[CallerEvidence]:
    """Find up to ``k`` representative call sites of ``fn_name``.

    Selection priority:
      1. Sites in distinct files come first.
      2. Within ties, non-test paths beat test paths.
      3. Within ties, earlier lines win (stable ordering).

    Each result has ±``context_radius`` lines of surrounding source.
    ``candidate_fn_names`` (optional) enables enclosing-function lookup
    when supplied; pass ``set(parsed_file.functions.keys())`` for the
    target file's union with other harvested files.
    """
    if not fn_name:
        return []
    call_rx = re.compile(rf"\b{re.escape(fn_name)}\s*\(")
    candidate_fn_names = candidate_fn_names or set()

    # (priority_tuple, file, line_idx, source_lines)
    hits: list[tuple[tuple, str, int, list[str]]] = []

    for path in corpus_paths:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        source_lines = text.splitlines()
        in_block_comment = False
        is_test = _is_test_path(str(path))
        brace_depth = 0  # tracks { } nesting at TU scope
        for idx, raw_line in enumerate(source_lines):
            line = raw_line
            # Cheap block-comment tracking.
            if in_block_comment:
                if "*/" in line:
                    in_block_comment = False
                continue
            if "/*" in line and "*/" not in line:
                in_block_comment = True
                continue
            if _line_is_comment_only(line):
                # Still need to track braces inside comments? No — they
                # don't affect C scope. Continue past.
                continue
            stripped = _strip_string_literals(line)
            # Match BEFORE adjusting brace depth — the call_rx position
            # is evaluated at the depth where the function-name token
            # appears, which is the same depth as the line's opening brace
            # for definition lines (so they get classified correctly).
            m = call_rx.search(stripped)
            # Compute the brace depth AT the position of the match:
            # depth at line start + opens-before-match - closes-before-match.
            if m is not None:
                depth_at_match = brace_depth + (
                    stripped[: m.start()].count("{")
                    - stripped[: m.start()].count("}")
                )
            else:
                depth_at_match = brace_depth
            # Update the running depth for the next line.
            brace_depth += stripped.count("{") - stripped.count("}")
            if m is None:
                continue
            if depth_at_match == 0:
                # TU scope — this is a declaration or definition, not a
                # call. Skip in either case. (Calls at TU scope happen
                # only in initializers like `int x = foo();`, which would
                # have `=` before `foo` and depth still 0 — those are
                # rare and the loss is acceptable.)
                continue
            prio = (
                0 if not is_test else 1,   # non-test first
                str(path),                  # then by file (stable distinct-file pref)
                idx,                        # then by earliest line
            )
            hits.append((prio, str(path), idx, source_lines))

    if not hits:
        return []

    # Deduplicate per (file, enclosing-function) heuristically by file
    # first: prefer one hit per file before taking second hits.
    hits_by_file: dict[str, list[tuple]] = {}
    for h in hits:
        hits_by_file.setdefault(h[1], []).append(h)

    # Round-robin pick: one from each file, then a second pass, etc.
    picks: list[tuple] = []
    file_order = sorted(hits_by_file.keys(),
                        key=lambda f: (1 if _is_test_path(f) else 0, f))
    round_idx = 0
    while len(picks) < k and any(
        len(hits_by_file[f]) > round_idx for f in file_order
    ):
        for f in file_order:
            if len(picks) >= k:
                break
            if len(hits_by_file[f]) > round_idx:
                picks.append(hits_by_file[f][round_idx])
        round_idx += 1

    out: list[CallerEvidence] = []
    for _prio, file_path, idx, source_lines in picks:
        lo = max(0, idx - context_radius)
        hi = min(len(source_lines), idx + context_radius + 1)
        ctx = source_lines[lo:hi]
        encl = _enclosing_function_for_line(source_lines, idx, candidate_fn_names)
        out.append(CallerEvidence(
            file=file_path,
            line=idx + 1,
            enclosing_function=encl,
            context_lines=ctx,
            call_line_text=source_lines[idx],
        ))
    return out


# ---------- vtable-registration (address-taken) harvest ---------------------

# When a function is only reached via a function-pointer callback (e.g.
# `rb_ops.rbto_compare_key = cmp_key_mbs`), `harvest_callers` returns
# zero direct call sites. The registration site is the relevant evidence
# — it tells us what protocol the framework imposes on the callback.


def harvest_address_taken_sites(
    fn_name: str,
    corpus_paths: list[Path],
    *,
    k: int = 3,
    context_radius: int = 6,
) -> list[CallerEvidence]:
    """Find sites where ``fn_name`` is mentioned *without* parentheses
    immediately after — i.e., the address is taken (callback registration,
    function-pointer init, vtable assignment).

    These are weaker evidence than direct call sites (they show what the
    function is registered AS but not what arguments will reach it), so
    they live in their own bucket and the prompt should reference them
    differently. ``k`` defaults to 3 since the same function is rarely
    registered in many places.
    """
    if not fn_name:
        return []
    # Word-boundary match for fn_name NOT followed by `(`.
    addr_rx = re.compile(rf"\b{re.escape(fn_name)}\b(?!\s*\()")

    hits: list[CallerEvidence] = []
    for path in corpus_paths:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        source_lines = text.splitlines()
        in_block_comment = False
        for idx, raw_line in enumerate(source_lines):
            if in_block_comment:
                if "*/" in raw_line:
                    in_block_comment = False
                continue
            if "/*" in raw_line and "*/" not in raw_line:
                in_block_comment = True
                continue
            if _line_is_comment_only(raw_line):
                continue
            stripped = _strip_string_literals(raw_line)
            if not addr_rx.search(stripped):
                continue
            # Skip the function's own forward declaration / definition
            # line; those have `(` right after the name (filtered above)
            # OR have `static`/return-type prefix exactly matching a sig.
            if re.match(rf"^\s*(static|extern)?[\w\s\*]*\b{re.escape(fn_name)}\s*[,;)]?\s*$", stripped):
                # Bare `fn_name;` declaration. Skip.
                if stripped.strip().rstrip(";").strip() == fn_name:
                    continue
            lo = max(0, idx - context_radius)
            hi = min(len(source_lines), idx + context_radius + 1)
            hits.append(CallerEvidence(
                file=str(path),
                line=idx + 1,
                enclosing_function=None,
                context_lines=source_lines[lo:hi],
                call_line_text=source_lines[idx],
            ))
            if len(hits) >= k:
                return hits
    return hits


# ---------- doc-annotation parsing ------------------------------------------

# Recognises:
#   /** ... */   javadoc / doxygen block comment
#   /*! ... */   doxygen-qt style
# preceding a function definition. We don't bother with single-line
# /// or //! styles; libarchive uses block comments.

_DOC_BLOCK_RX = re.compile(r"/\*[\*!](.*?)\*/", re.DOTALL)
_DOC_ANNOTATION_RX = re.compile(
    r"[@\\](param|pre|post|returns?|brief)\b\s*([^\n]*)", re.IGNORECASE
)
_PARAM_NAME_RX = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(\w+)")


def parse_doc_annotations(
    parsed_file: "ParsedCFile",
    fn_name: str,
) -> list[DocClause]:
    """Extract Doxygen annotations from the doc-comment immediately
    preceding the function definition for ``fn_name`` in ``parsed_file``.

    Returns an empty list if no doc comment is present.
    """
    fn_def = parsed_file.function_definitions.get(fn_name) if hasattr(
        parsed_file, "function_definitions"
    ) else None
    if not fn_def:
        return []
    try:
        source = parsed_file.preprocessed_source or Path(parsed_file.path).read_text(
            errors="replace"
        )
    except OSError:
        return []

    # Locate the function definition's position in the source.
    pos = source.find(fn_def[:80])  # match by first 80 chars (cheap)
    if pos < 0:
        return []
    # Walk backward over whitespace/comments until we find a comment
    # block or hit non-comment code (in which case there's no doc).
    head = source[:pos]
    # Find the last comment block before `pos`. Allow up to 2 blank
    # lines between the comment and the function. Cheap pattern:
    # match the closing `*/` followed by optional whitespace + nothing
    # else before `pos`.
    last_close = head.rfind("*/")
    if last_close < 0:
        return []
    between = head[last_close + 2 : pos]
    # Reject if non-whitespace exists between the comment and the def.
    if between.strip():
        return []
    # Find the matching open for the closing `*/`.
    last_open = head.rfind("/*", 0, last_close)
    if last_open < 0:
        return []
    block = head[last_open : last_close + 2]

    m = _DOC_BLOCK_RX.search(block)
    if not m:
        return []
    body = m.group(1)
    clauses: list[DocClause] = []
    for am in _DOC_ANNOTATION_RX.finditer(body):
        kind = am.group(1).lower()
        if kind == "return":
            kind = "returns"
        text = am.group(2).strip().lstrip("*").strip()
        param = None
        if kind == "param":
            pm = _PARAM_NAME_RX.match(text)
            if pm:
                param = pm.group(1)
                text = text[pm.end():].strip()
        clauses.append(DocClause(raw_text=text, annotation_type=kind, param_name=param))
    return clauses


# ---------- universal-pattern seeding ---------------------------------------


def seed_from_universal_patterns(
    func_info: "FunctionInfo",
    *,
    struct_definitions: Optional[dict] = None,
    cbmc_unwind: int = 4,
) -> list[SeedClause]:
    """Wrap universal_contracts.derive_universal_precondition and tag
    each emitted clause with its source pattern.

    The underlying function returns a single ``&&``-joined string; we
    split it on `` && `` to recover individual clauses. Pattern name is
    best-effort — we use ``derive_contract_summary`` where available and
    fall back to the generic tag ``"signature_pattern"``.
    """
    try:
        from bmc_agent.universal_contracts import (
            derive_universal_precondition,
            derive_contract_summary,
        )
    except ImportError:
        return []
    try:
        pre = derive_universal_precondition(
            func_info,
            struct_definitions=struct_definitions,
            cbmc_unwind=cbmc_unwind,
        )
    except Exception as exc:
        logger.debug("derive_universal_precondition failed for %s: %r",
                     func_info.name, exc)
        return []
    if not pre or pre.strip() == "true":
        return []

    # Build a reverse-lookup so we can tag known categories.
    pattern_for_clause: dict[str, str] = {}
    try:
        summary = derive_contract_summary(func_info)
        for cat, clauses in summary.items():
            for c in clauses:
                pattern_for_clause[c.strip()] = cat
    except Exception:
        pass

    out: list[SeedClause] = []
    for clause in (c.strip() for c in pre.split(" && ") if c.strip()):
        tag = pattern_for_clause.get(clause, "signature_pattern")
        out.append(SeedClause(clause=clause, pattern_name=tag))
    return out


# ---------- orchestration entry point ---------------------------------------


def gather_evidence_bundle(
    func_info: "FunctionInfo",
    parsed_file: "ParsedCFile",
    corpus_paths: list[Path],
    *,
    k_callers: int = 5,
    context_radius: int = 8,
    struct_definitions: Optional[dict] = None,
    cbmc_unwind: int = 4,
    candidate_fn_names: Optional[set[str]] = None,
) -> EvidenceBundle:
    """Run all three evidence sources and package the result.

    Cheap to call — caller harvest is the only non-trivial cost (one
    file read per corpus_paths entry). For libarchive's 7-file working
    corpus, total cost per function is ~tens of milliseconds.
    """
    callers = harvest_callers(
        func_info.name,
        corpus_paths,
        k=k_callers,
        context_radius=context_radius,
        candidate_fn_names=candidate_fn_names,
    )
    # When no direct callers exist (vtable-dispatch case), fall back to
    # address-taken sites. Bookkeeping them separately lets the prompt
    # weight them differently — they show what the function is registered
    # AS, not what arguments will reach it.
    addr_sites: list[CallerEvidence] = []
    if not callers:
        addr_sites = harvest_address_taken_sites(
            func_info.name, corpus_paths, k=3, context_radius=context_radius
        )
    return EvidenceBundle(
        fn_name=func_info.name,
        callers=callers,
        address_taken_sites=addr_sites,
        doc_annotations=parse_doc_annotations(parsed_file, func_info.name),
        seed_clauses=seed_from_universal_patterns(
            func_info,
            struct_definitions=struct_definitions,
            cbmc_unwind=cbmc_unwind,
        ),
    )
