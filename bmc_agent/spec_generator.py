"""
Phase 1: Spec Generator [AGENTIC].

LLM agent that plans spec generation across the call graph, drafts specs in the
DSL, validates with a parser, retries on failure, and cross-checks between
caller-heavy and impl-heavy sources to flag disagreements.

Top-down caller-driven paradigm (FM-Agent §4.2-4.3):
1. Parse the C file to extract function info and call graph.
2. Build SCCs using Kosaraju's algorithm.
3. Condense the call graph into a DAG of SCCs.
4. Compute a layered topological sort (Algorithm 1 from proposal).
5. Process layers top-down, generating specs concurrently within each layer.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

from bmc_agent.artifacts import ArtifactStore
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient, LLMError
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile
from bmc_agent.source_parser import detect_language, parse_source_file
from bmc_agent.prompts import (
    CALLER_HEAVY_SPEC_PROMPT,
    DSL_GRAMMAR,
    ENTRY_SPEC_PROMPT,
    EXPECTED_SPEC_PROMPT,
    IMPL_HEAVY_SPEC_PROMPT,
    INTERNAL_SPEC_PROMPT,
    SPEC_DISAGREEMENT_PROMPT,
    SPEC_SYSTEM_PROMPT,
    THREAT_MODEL_CONTEXT,
    spec_system_prompt_for,
)
from bmc_agent.spec import Spec, SpecStatus, merge_specs

logger = get_logger("spec_generator")

# ---------------------------------------------------------------------------
# Weak (fallback) spec
# ---------------------------------------------------------------------------

_FALLBACK_PRECONDITION = "true"
_FALLBACK_POSTCONDITION = "true"


def _fallback_spec(func_name: str, reason: str = "") -> Spec:
    """Return a trivially weak spec and log a warning."""
    msg = f"Falling back to weak spec for '{func_name}'"
    if reason:
        msg += f": {reason}"
    logger.warning(msg)
    spec = Spec(
        function_name=func_name,
        precondition=_FALLBACK_PRECONDITION,
        postcondition=_FALLBACK_POSTCONDITION,
        status=SpecStatus.GENERATED,
    )
    # Tag the spec so callers can detect fallback
    spec.__dict__["fallback"] = True
    return spec


def _stub_spec(func_name: str) -> Spec:
    """Return a stub spec for an external/library function not defined in source."""
    logger.debug("Creating stub spec for external function '%s'", func_name)
    spec = Spec(
        function_name=func_name,
        precondition="true",
        postcondition="true",
        status=SpecStatus.GENERATED,
    )
    spec.__dict__["fallback"] = True
    spec.__dict__["stub"] = True
    return spec


def _permissive_spec(
    func_name: str,
    func_info=None,
    with_contracts: bool = False,
    struct_definitions: dict | None = None,
    cbmc_unwind: int = 4,
) -> Spec:
    """Permissive spec for bmc-agent-lite mode.

    Skips the LLM spec_gen call entirely. The harness generator still wires
    nondet inputs subject to the global flags (``raw_bytes``,
    ``infer_array_param_bounds``, etc.), and CBMC's built-in checks
    (``--bounds-check``, ``--pointer-check``, ``--signed-overflow-check``)
    still fire. The LLM budget shifts to Phase 3 (realism + classifier),
    where it adds real value rather than parroting the function body.

    When ``with_contracts=True`` and ``func_info`` is supplied, the
    precondition is enriched with deterministic *universal contracts*
    derived from parameter names + types — no LLM call. Today's
    universal contracts only emit paired-pointer ordering
    (``start <= end``, etc.); the existing
    ``_detect_paired_pointers`` in ``harness_generator.py`` picks up
    the clause and allocates a single shared backing buffer per pair,
    eliminating the textbook caller-contract-slip FP class that
    dominates lite-mode noise on userland libraries.
    """
    precondition = "true"
    if with_contracts and func_info is not None:
        try:
            from bmc_agent.universal_contracts import derive_universal_precondition
            derived = derive_universal_precondition(
                func_info,
                struct_definitions=struct_definitions,
                cbmc_unwind=cbmc_unwind,
            )
            if derived and derived != "true":
                precondition = derived
        except Exception:
            # Universal-contract derivation must never crash spec gen;
            # fall back to the permissive default.
            precondition = "true"
    spec = Spec(
        function_name=func_name,
        precondition=precondition,
        postcondition="true",
        status=SpecStatus.GENERATED,
    )
    spec.__dict__["lite"] = True
    return spec


# ---------------------------------------------------------------------------
# SCC computation (Kosaraju's algorithm)
# ---------------------------------------------------------------------------


def _kosaraju_sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    """
    Compute strongly connected components using Kosaraju's algorithm.

    Parameters
    ----------
    graph:
        Adjacency list: node -> set of successors.

    Returns
    -------
    List of SCCs (each SCC is a list of node names), in reverse topological
    order of the condensed DAG (i.e., the first SCC has no incoming edges
    from later SCCs).
    """
    nodes = list(graph.keys())
    # Ensure all nodes referenced as callees are in the graph
    for callees in list(graph.values()):
        for c in callees:
            if c not in graph:
                nodes.append(c)
    nodes = list(dict.fromkeys(nodes))  # deduplicate while preserving order

    # Build reverse graph
    rev: dict[str, set[str]] = {n: set() for n in nodes}
    for u in nodes:
        for v in graph.get(u, set()):
            if v in rev:
                rev[v].add(u)

    # Pass 1: DFS on original graph, record finish order
    visited: set[str] = set()
    finish_order: list[str] = []

    def dfs1(node: str) -> None:
        stack = [(node, iter(graph.get(node, set())))]
        visited.add(node)
        while stack:
            u, it = stack[-1]
            try:
                v = next(it)
                if v not in visited and v in rev:  # only visit known nodes
                    visited.add(v)
                    stack.append((v, iter(graph.get(v, set()))))
            except StopIteration:
                finish_order.append(u)
                stack.pop()

    for n in nodes:
        if n not in visited:
            dfs1(n)

    # Pass 2: DFS on reverse graph in reverse finish order
    visited2: set[str] = set()
    sccs: list[list[str]] = []

    def dfs2(node: str, component: list[str]) -> None:
        stack = [node]
        visited2.add(node)
        while stack:
            u = stack.pop()
            component.append(u)
            for v in rev.get(u, set()):
                if v not in visited2:
                    visited2.add(v)
                    stack.append(v)

    for n in reversed(finish_order):
        if n not in visited2:
            comp: list[str] = []
            dfs2(n, comp)
            sccs.append(comp)

    return sccs


# ---------------------------------------------------------------------------
# Condensed DAG and layered topological sort
# ---------------------------------------------------------------------------


def _build_generation_order(call_graph: dict[str, set[str]]) -> list[list[str]]:
    """
    Compute the layered topological sort of functions.

    Algorithm:
    1. Compute SCCs.
    2. Condense the call graph into a DAG of SCCs.
    3. BFS/Kahn's algorithm on the condensed DAG to get layers.
    4. Flatten back to function names.

    Layer 1 = entry functions (no callers in the call graph).
    Layer 2 = functions called only by layer-1 functions.
    etc.

    Parameters
    ----------
    call_graph:
        Mapping caller -> set of callee names (only defined functions).

    Returns
    -------
    List of layers; each layer is a list of function names.
    """
    if not call_graph:
        return []

    sccs = _kosaraju_sccs(call_graph)
    # Map each node to its SCC index
    node_to_scc: dict[str, int] = {}
    for scc_idx, scc in enumerate(sccs):
        for node in scc:
            node_to_scc[node] = scc_idx

    num_sccs = len(sccs)
    # Build condensed DAG: scc_edges[i] = set of scc indices that scc i calls
    scc_edges: dict[int, set[int]] = {i: set() for i in range(num_sccs)}
    for u in call_graph:
        if u not in node_to_scc:
            continue
        u_idx = node_to_scc[u]
        for v in call_graph[u]:
            if v in node_to_scc:
                v_idx = node_to_scc[v]
                if v_idx != u_idx:
                    scc_edges[u_idx].add(v_idx)

    # Compute in-degrees for condensed DAG
    in_degree: dict[int, int] = {i: 0 for i in range(num_sccs)}
    for i, successors in scc_edges.items():
        for j in successors:
            in_degree[j] += 1

    # Kahn's algorithm for layered topological sort (callers before callees)
    # Layer 0 = SCCs with no incoming edges (entry SCCs)
    layers_scc: list[list[int]] = []
    queue: deque[int] = deque(i for i in range(num_sccs) if in_degree[i] == 0)

    while queue:
        current_layer_sccs = list(queue)
        queue.clear()
        layers_scc.append(current_layer_sccs)
        for scc_idx in current_layer_sccs:
            for successor in scc_edges[scc_idx]:
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    queue.append(successor)

    # Flatten: only keep functions that are in the original call_graph keys
    defined_funcs = set(call_graph.keys())
    layers: list[list[str]] = []
    for scc_layer in layers_scc:
        layer_funcs: list[str] = []
        for scc_idx in scc_layer:
            for node in sccs[scc_idx]:
                if node in defined_funcs:
                    layer_funcs.append(node)
        if layer_funcs:
            layers.append(layer_funcs)

    return layers


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def _relax_postcondition_for_error_paths(post: str, body: str, func_name: str) -> str:
    """
    Soften over-strict postconditions when the function body has explicit
    error-return paths.

    Empirical pattern from this session's bounty runs: the LLM frequently
    generates postconditions like ``result != NULL`` or ``result > 0``
    while the function body has explicit ``return NULL;`` / ``return -1;``
    error paths. CBMC then flags every error-path execution as a
    postcondition violation, even though the function is doing exactly
    what its contract requires.

    Strategy: when the body contains explicit returns of common error
    sentinels (NULL, negative integers, UPPER_SNAKE error enums), and the
    postcondition would assert against those sentinels, OR the existing
    postcondition with a clause that permits the sentinel.

    The softening preserves the SAFETY content of the postcondition on
    success paths (the original clauses still have to hold when the
    function doesn't return the error sentinel) while letting CBMC
    explore error paths without flagging them as bugs.
    """
    if not body or not post:
        return post

    # Find explicit ``return X;`` statements. Constrain to single-line
    # forms with simple expressions to avoid eating multi-line returns
    # or returns of complex expressions.
    error_returns: set[str] = set()
    for m in re.finditer(r"\breturn\s+([^;\n]{1,40});", body):
        val = m.group(1).strip()
        if val in ("NULL", "0", "false", "FALSE", "-1"):
            error_returns.add(val)
        elif re.match(r"^-\d{1,5}$", val):
            error_returns.add(val)
        elif re.match(r"^[A-Z][A-Z0-9_]{2,}$", val):
            # Uppercase-snake identifier, likely an error enum
            # (CURLUE_OUT_OF_MEMORY, ASN1_R_TOO_LONG, NGHTTP2_ERR_HEADER_COMP).
            # We don't know the integer value, but record it as a marker
            # that some non-trivial error sentinel exists.
            error_returns.add(val)

    if not error_returns:
        return post

    softened = post
    notes: list[str] = []

    # Pattern 1: ``result != NULL`` postcondition where body has
    # ``return NULL;``. OR-in ``result == NULL`` to permit the error path.
    if "NULL" in error_returns and re.search(r"\bresult\s*!=\s*NULL\b", post):
        if "result == NULL" not in post:
            softened = f"(result == NULL) || ({softened})"
            notes.append("permit result==NULL")

    # Pattern 2: ``result > 0`` / ``result >= 0`` postcondition where body
    # has ``return -1;`` or any explicit negative-integer return.
    # OR-in ``result < 0`` to permit the error path.
    has_negative_return = any(
        v == "-1" or re.match(r"^-\d", v) for v in error_returns
    )
    if has_negative_return and re.search(r"\bresult\s*>\s*0\b", post):
        if "result < 0" not in post and "result <= 0" not in post:
            softened = f"(result < 0) || ({softened})"
            notes.append("permit result<0")
    elif has_negative_return and re.search(r"\bresult\s*>=\s*0\b", post):
        if "result < 0" not in post and "result <= 0" not in post:
            softened = f"(result < 0) || ({softened})"
            notes.append("permit result<0")

    # Pattern 3: function has UPPER_SNAKE error returns (enum sentinels);
    # if postcondition asserts ``result == K`` for a narrow set, broaden
    # to also permit the sentinel.  We don't know the int values, so we
    # softly permit any value the body can return.  Conservative: only
    # apply when the postcondition is a disjunction of equalities and
    # there's at least one UPPER_SNAKE error return.
    upper_snake_returns = [v for v in error_returns if re.match(r"^[A-Z]", v)]
    if upper_snake_returns and re.match(
        r"^\s*\(?\s*result\s*==\s*\S+\s*(\)|\)\s*\|\|\s*\(?\s*result\s*==\s*\S+\s*\)?)*\s*$",
        post,
    ):
        # Append each error sentinel as an alternative.
        extra = " || ".join(f"result == {v}" for v in sorted(upper_snake_returns))
        softened = f"({softened}) || ({extra})"
        notes.append(f"permit error sentinels {sorted(upper_snake_returns)[:3]}")

    if softened != post:
        logger.info(
            "Spec quality: softened postcondition for '%s' (%s)",
            func_name, "; ".join(notes),
        )
    return softened


class _ParsedSpecBase(NamedTuple):
    precondition: str
    postcondition: str
    pre_validity: str = ""
    pre_protocol: str = ""


class ParsedSpec(_ParsedSpecBase):
    """Result of parsing the LLM's JSON spec response.

    ``precondition`` / ``postcondition`` are the legacy flat fields (still
    populated for back-compat). ``pre_validity`` / ``pre_protocol`` are
    optional structured pre-clause splits the LLM may emit alongside the
    flat ``precondition`` — used as internal organisation (v2's evidence
    trust scoring + feedback loop drop-priority). Both default to ``""`` so
    the downstream ``Spec.split_precondition`` falls back to the
    classifier.

    Back-compat: equality with a plain ``(pre, post)`` 2-tuple still
    holds when the structured fields are empty. Phase 2 of the
    validity/protocol split would otherwise have broken ~15 existing
    parser tests that assert ``out == ("true", "result >= 0")`` etc.
    """

    def __eq__(self, other: object) -> bool:  # noqa: D401
        if isinstance(other, tuple) and not isinstance(other, _ParsedSpecBase):
            if len(other) == 2 and not self.pre_validity and not self.pre_protocol:
                return (self.precondition, self.postcondition) == other
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __iter__(self):
        # Back-compat: when the structured split fields are empty,
        # iteration / unpacking yields just (precondition, postcondition)
        # so existing call sites can keep doing ``pre, post = result``.
        # When either split field is non-empty, all four elements are
        # yielded so the structured fields aren't silently dropped.
        if not self.pre_validity and not self.pre_protocol:
            yield self.precondition
            yield self.postcondition
            return
        yield from super().__iter__()

    # NamedTuple subclasses inherit __hash__ from tuple, but redefining
    # __eq__ in CPython implicitly sets __hash__ to None unless we
    # restore it explicitly.
    __hash__ = _ParsedSpecBase.__hash__


def _parse_llm_spec_response(
    response: str,
    func_name: str,
    simple_specs: bool = False,
) -> Optional[ParsedSpec]:
    """
    Parse LLM JSON response into (precondition, postcondition).

    If the response contains an optional ``functional_spec`` field (Phase 1
    behavioral spec — Rust/C boolean expression specifying what the function
    SHOULD compute, not just what makes it safe), AND it into the
    postcondition so existing harness gen, classification, and refinement
    paths consume it without any further changes.

    When ``simple_specs`` is True (cargo-mode default), the ``functional_spec``
    field is dropped entirely. Rationale: Claude routinely emits functional
    specs as nested ``iter().fold(...).wrapping_mul(...)`` reference-
    equivalence expressions that compile fine but cause Kani's SMT solver
    to hang for minutes on trivial functions. Verified manually on
    adler::adler32_slice: full spec → cargo kani hangs >60s; simplified
    spec → 1.3s verify of 482 properties. The remaining defensive checks
    (pre constrains inputs, post bounds the result range) still catch the
    important bug classes (slice OOB, overflow), they just don't try to
    prove algorithmic equivalence.

    Returns None if parsing fails.
    """
    text = response.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        inner = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not lines[0].startswith("```"):
                inner.append(line)
        text = "\n".join(inner).strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: reasoning models (K2 Think etc.) sometimes bracket the JSON
        # with a trailing prose line ("Hope this helps!", "Note: …") even after
        # the </think> strip. Extract the first balanced top-level JSON object.
        obj = _extract_first_json_object(text)
        if obj is not None:
            try:
                data = json.loads(obj)
            except json.JSONDecodeError:
                data = None

    if data is not None:
        pre = data.get("precondition", "").strip()
        post = data.get("postcondition", "").strip()
        # K2 Think regularly emits an over-strict postcondition of the shape
        # ``(result == E) && E`` -- a correct reference-equivalence clause
        # ANDed with the same predicate standing on its own. The standalone
        # clause turns the postcondition into "the input satisfies E" which
        # is wrong: the function is defined for ALL inputs, returning
        # whatever E evaluates to. Strip the redundant clause so the harness
        # gets the correct ``result == E`` instead.
        post = _strip_redundant_input_clause(post)
        # Optional functional spec (Phase 1). LLM may either omit the field,
        # leave it empty, or set it to "true" when no useful functional
        # property is derivable. Skip in all those cases.
        # In simple_specs mode (cargo-mode), drop functional specs entirely
        # to avoid Kani SMT-solver hangs on iter().fold-style expressions.
        functional = "" if simple_specs else (data.get("functional_spec") or "").strip()
        # Earlier we blanket-dropped any spec containing ``old(...)``
        # because the Kani harness couldn't see pre-call state. The
        # backend now snapshots ``old(EXPR)`` into ``_pre_N`` bindings
        # before the function call, so these specs compile and verify
        # correctly. Keep the drop ONLY for pathological cases the
        # snapshot logic can't handle (currently: none we've hit).
        if functional and functional.lower() not in ("true", "1", "n/a", "none"):
            # AND the functional spec into the postcondition. Existing
            # postcondition stays in place (defensive bug-class clauses);
            # the functional clause adds the "behaves correctly" part. If
            # post was "true", the AND collapses to just the functional spec.
            if post in ("", "true"):
                post = functional
            else:
                post = f"({post}) && ({functional})"
        if pre and post:
            # Phase-2 structured split: when the LLM emits pre_validity /
            # pre_protocol alongside the flat precondition, surface them
            # for internal organisation (v2 evidence trust scoring +
            # feedback-loop drop-priority). Both default to "" —
            # Spec.split_precondition then falls back to the classifier
            # in spec.py.
            pre_validity = (data.get("pre_validity") or "").strip()
            pre_protocol = (data.get("pre_protocol") or "").strip()
            return ParsedSpec(
                precondition=pre,
                postcondition=post,
                pre_validity=pre_validity,
                pre_protocol=pre_protocol,
            )

    return None


def _split_top_level_and(post: str) -> Optional[tuple[str, str]]:
    """Split *post* on the rightmost top-level ``&&`` operator.

    Top-level means brace/paren/bracket depth is zero. String literals and
    char literals are skipped so a ``&&`` inside a quoted string doesn't
    trigger the split. Returns ``(lhs, rhs)`` with the operator removed,
    or ``None`` if no top-level ``&&`` exists.
    """
    depth = 0
    i = 0
    in_str = None  # None, '"', or "'"
    esc = False
    last_split = -1
    while i < len(post):
        ch = post[i]
        if in_str is not None:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "&" and i + 1 < len(post) and post[i + 1] == "&":
            last_split = i
            i += 2
            continue
        i += 1
    if last_split < 0:
        return None
    return post[:last_split].rstrip(), post[last_split + 2:].lstrip()


def _strip_outer_parens(s: str) -> str:
    """Strip one layer of balanced outer parentheses if they wrap the whole expr."""
    s = s.strip()
    if not (s.startswith("(") and s.endswith(")")):
        return s
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i < len(s) - 1:
                return s  # the first ``(`` closed before the end -> not a single wrap
    return s[1:-1].strip()


def _normalise_expr(s: str) -> str:
    """Cheap structural equality token-strip: collapse whitespace, strip outer parens."""
    s = _strip_outer_parens(s.strip())
    # Collapse interior whitespace
    return " ".join(s.split())


def _strip_redundant_input_clause(post: str) -> str:
    """Strip the K2 ``(result == E) && E`` over-strictness pattern.

    K2 Think functional-spec gen frequently emits postconditions of the shape::

        (result == (P(x)))    &&    (P(x))

    The first clause is the correct reference-equivalence spec for a pure
    predicate. The second standalone clause turns the spec into "the input
    satisfies P", which over-constrains the function to only verify on
    "happy-path" inputs and produces a spurious CEX whenever Kani picks an
    input that legitimately makes the function return ``false``. Three
    instances observed live in the K2 CCC sweep (is_ident_start_byte,
    is_ident_cont, …), all SPURIOUS.

    The rewrite is deterministic and zero-cost: if ``post`` matches one of
    these shapes, return just the reference-equivalence clause. Otherwise
    return ``post`` unchanged.

    Supported shapes (both orderings):
        (result == E) && E        ->  result == E
        E && (result == E)        ->  result == E
        (result == E1 && extra)   left alone -- conservative
    """
    if not post or "&&" not in post:
        return post

    split = _split_top_level_and(post)
    if split is None:
        return post
    lhs_raw, rhs_raw = split
    lhs = _strip_outer_parens(lhs_raw)
    rhs = _strip_outer_parens(rhs_raw)

    # Case A: lhs is ``result == E`` and rhs is ``E``
    for prefix in ("result == ", "result==", "(result) == ", "result.is_some() == "):
        if lhs.startswith(prefix):
            e_in_lhs = _normalise_expr(lhs[len(prefix):])
            if e_in_lhs == _normalise_expr(rhs) and e_in_lhs:
                return lhs_raw

    # Case B: rhs is ``result == E`` and lhs is ``E``
    for prefix in ("result == ", "result==", "(result) == "):
        if rhs.startswith(prefix):
            e_in_rhs = _normalise_expr(rhs[len(prefix):])
            if e_in_rhs == _normalise_expr(lhs) and e_in_rhs:
                return rhs_raw

    return post


def _extract_first_json_object(text: str) -> Optional[str]:
    """Return the first top-level ``{...}`` substring whose braces balance.

    Used as a fallback when ``json.loads`` on the full response fails because
    a reasoning model wrapped the JSON in surrounding prose. Counts brace
    depth while respecting string literals and escape sequences so that braces
    inside string values don't throw off the count. Returns ``None`` if no
    balanced object is found.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        i = start
        in_str = False
        esc = False
        while i < len(text):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]
            i += 1
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# SpecGenerator
# ---------------------------------------------------------------------------


class SpecGenerator:
    """
    Generates specifications for all functions in a C source file.

    Uses the top-down caller-driven paradigm:
    - Entry functions (no callers) get specs from implementation + domain knowledge.
    - Internal functions get specs derived from what their callers expect.
    """

    def __init__(self, config: Config, llm: LLMClient, store: ArtifactStore) -> None:
        self.config = config
        self.llm = llm
        self.store = store
        # System prompt is set per-generate_specs() call based on input
        # language. Initialised to the C prompt so test doubles that bypass
        # generate_specs() (calling internal _generate_* helpers directly)
        # still get a well-formed system prompt.
        self._spec_system_prompt: str = SPEC_SYSTEM_PROMPT

    def _complete_with_vacuous_critique(
        self,
        user_prompt: str,
        func: "FunctionInfo",
    ) -> Optional[ParsedSpec]:
        """Run a spec-gen prompt, then re-prompt once if the first response is
        a vacuous ``true`` / ``true`` spec on a non-trivial function body.

        Reasoning-model providers (K2 Think on the openai-compatible path)
        emit ``pre=true, post=true`` on ~85% of CCC functions in a default
        generation pass: the model burns the bulk of its completion budget
        on a ``<think>...`` trace and then defaults to the safest answer.
        For functions whose body has any real arithmetic, indexing, or
        control flow, this drops a substantial chunk of potential functional
        bugs because Kani verifies trivially against ``true``.

        The critique pass shows the model its own vacuous output and asks it
        to identify at least one algebraic invariant of the return value.
        Costs a 2x LLM call for the fraction of functions where the first
        response was vacuous; on the Anthropic provider this codepath is a
        near no-op (Claude rarely returns vacuous defaults).

        Returns the parsed ``(pre, post)`` pair from whichever attempt
        produced a richer spec, or ``None`` if neither parsed.
        """
        response = self.llm.complete(
            self._spec_system_prompt, user_prompt, role="spec_gen",
        )
        first = _parse_llm_spec_response(response, func.name, simple_specs=getattr(self.config, 'simple_specs', False))
        if first is None:
            return None
        pre, post = first.precondition, first.postcondition

        body = getattr(func, "body", "") or ""
        # "Trivial" = a one-liner with no inner block: short AND no control flow.
        # Anything with an `if`, `match`, loop, or even a let-then-return has a
        # second brace and warrants the critique pass.
        trivial_body = len(body) < 40 and body.count("{") <= 1
        # Vacuous patterns we've observed K2 emit:
        #   - pre == "true" AND post == "true" (the giveup-trivial pattern)
        #   - pre == "false" (assume(false) prunes all paths -> any postcondition holds)
        #   - post == "false" with pre also "false" (already covered by pre=="false" but worth flagging)
        # A pre of "false" makes the harness trivially verify regardless of the
        # function under test, because Kani sees an unreachable assertion. We
        # observed this on byteorder::default where K2 emitted both pre and
        # post as "false".
        is_trivially_true = pre.strip() in ("true", "") and post.strip() in ("true", "")
        is_unreachable = pre.strip() == "false"
        is_vacuous = is_trivially_true or is_unreachable
        if not is_vacuous or trivial_body:
            return first

        # Only run the critique on the K2/openai path -- on Anthropic, vacuous
        # output is already rare and the extra call would just double cost.
        provider = self.config.resolved_provider() if hasattr(self.config, "resolved_provider") else "anthropic"
        if provider != "openai":
            return first

        critique_prompt = (
            "The spec you just produced was:\n"
            f'  precondition:  "{pre}"\n'
            f'  postcondition: "{post}"\n'
            "Both clauses are `true`, which trivially holds for ANY execution.\n"
            "That is not a useful spec for a function with a non-trivial body.\n\n"
            "Look at the function body again and identify AT LEAST ONE meaningful invariant:\n"
            "  * an algebraic identity on the return value (e.g. `result % align == 0`,\n"
            "    `result >= input.iter().min().unwrap_or(&0)`, `result.len() <= input.len()`)\n"
            "  * a reference-equivalence to an obvious specification expression\n"
            "    (e.g. `result == u16::from_le_bytes([data[offset], data[offset+1]])`,\n"
            "    `result == name.iter().fold(SEED, |acc, b| ...)`)\n"
            "  * a structural property (e.g. `result.is_some() == !haystack.is_empty()`,\n"
            "    `(result, advance).1 == 1 || (result, advance).1 == 3`)\n"
            "  * a precondition that the body assumes (e.g. `pos < buf.len()`,\n"
            "    `align.is_power_of_two()`, `n <= slice.len()`)\n\n"
            "Re-emit a JSON object with `precondition` and `postcondition` keys, possibly\n"
            "with an additional `functional_spec` field. Output ONLY the JSON object.\n\n"
            "The original request was:\n\n"
            + user_prompt
        )

        try:
            # Critique prompt embeds the original prompt verbatim, so it's
            # noticeably longer than the first call. K2 Think regularly
            # consumed the full 16384-token floor on the first attempt and
            # tripped finish_reason=length on the critique. Bump the cap
            # so the reasoning model has room to think AND emit the answer.
            critique_response = self.llm.complete(
                self._spec_system_prompt,
                critique_prompt,
                max_tokens=32768,
                role="spec_gen",
            )
        except LLMError as exc:
            logger.debug(
                "Vacuous-spec critique call failed for '%s': %s -- using first response",
                func.name, exc,
            )
            return first

        second = _parse_llm_spec_response(critique_response, func.name, simple_specs=getattr(self.config, 'simple_specs', False))
        if second is None:
            logger.debug(
                "Vacuous-spec critique produced unparseable response for '%s' -- keeping first",
                func.name,
            )
            return first
        pre2, post2 = second.precondition, second.postcondition
        # Accept the critique result only if it's strictly richer (at least one
        # clause is non-trivial). If the model insists on true/true, take that
        # as evidence that the function genuinely has no useful invariant
        # the LLM can articulate and don't churn further.
        if pre2.strip() not in ("true", "") or post2.strip() not in ("true", ""):
            logger.info(
                "Vacuous-spec critique upgraded '%s': pre=%r post=%r",
                func.name, pre2[:60], post2[:60],
            )
            return second
        logger.debug(
            "Vacuous-spec critique confirmed true/true for '%s'", func.name,
        )
        return first

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_specs(
        self,
        source_file: str,
        driver_name: str,
        domain_knowledge: str = "",
        source_text: Optional[str] = None,
        cross_file_caller_contexts: Optional[dict] = None,
    ) -> dict[str, Spec]:
        """
        Generate specs for all functions in source_file.

        Parameters
        ----------
        source_file:
            Path to the C source file.
        driver_name:
            Name of the driver (used for artifact storage).
        domain_knowledge:
            Optional domain knowledge string to pass to the LLM.
        source_text:
            If provided, parse this string instead of reading source_file from
            disk (use to pass preprocessed / include-expanded source so struct
            definitions and constructor bodies are visible to the spec generator).

        Returns
        -------
        Mapping of function_name -> Spec.
        """
        logger.info("Parsing source file: %s", source_file)
        parsed = parse_source_file(source_file, source_text=source_text)

        # If the input was a preprocessed translation unit (cpp ``# N "..."``
        # line directives present), drop functions that originated in
        # included headers. A Linux driver TU pulls in several thousand
        # ``static inline`` helpers from ``include/linux/*.h``; without
        # this filter, the pipeline tries to spec all of them and the run
        # is intractable. The first ``# N "..."`` directive in the file
        # identifies the original ``.c`` source — keep only functions
        # tagged with that origin. Plain ``.c`` input has no directives,
        # so this is a no-op there.
        primary = getattr(parsed, "primary_source", None)
        if primary and hasattr(parsed, "restrict_to_primary_source"):
            kept_before = len(parsed.functions)
            dropped = parsed.restrict_to_primary_source()
            if dropped:
                logger.info(
                    "Preprocessed TU detected (origin %s): kept %d functions, "
                    "dropped %d header-inlined helpers",
                    primary, kept_before - dropped, dropped,
                )

        # Select the language-appropriate system prompt for this run so
        # Rust input gets Rust-aware DSL notes (references, slices,
        # wrapping arithmetic) rather than C-flavored ones.  When
        # config.strict_dsl is set, swap in the strict-formal C
        # variant — bounty/CVE work needs single-C-expression specs
        # because prose mixing translates to vacuous verifications.
        language = detect_language(source_file)
        strict = bool(getattr(self.config, "strict_dsl", False))
        safety_only = bool(getattr(self.config, "safety_only", False))
        self._spec_system_prompt = spec_system_prompt_for(
            language, strict=strict, safety_only=safety_only,
        )
        logger.info(
            "Using %s%s%s spec system prompt",
            "strict-" if (strict and language == "c") else "",
            "safety-only " if safety_only else "",
            language,
        )

        self.store.init_driver(driver_name)

        # Only include functions defined in this file in the call graph
        defined_funcs = set(parsed.functions.keys())

        # Filter call graph to only include callees that are defined in source
        # (external callees get stub specs)
        filtered_call_graph: dict[str, set[str]] = {}
        for fn_name in defined_funcs:
            raw_callees = parsed.call_graph.get(fn_name, set())
            filtered_call_graph[fn_name] = raw_callees & defined_funcs

        logger.info(
            "Found %d functions: %s",
            len(defined_funcs),
            sorted(defined_funcs),
        )

        # Build the generation order
        layers = self._build_generation_order(filtered_call_graph)
        logger.info("Generation layers: %s", layers)

        # Generate stub specs for external callees
        all_specs: dict[str, Spec] = {}
        for fn_name in defined_funcs:
            raw_callees = parsed.call_graph.get(fn_name, set())
            for callee in raw_callees:
                if callee not in defined_funcs and callee not in all_specs:
                    all_specs[callee] = _stub_spec(callee)

        # Process layer by layer (top-down: entry functions first)
        for layer_idx, layer in enumerate(layers):
            logger.info("Processing layer %d: %s", layer_idx + 1, layer)
            is_entry_layer = layer_idx == 0

            # Determine which functions in this layer are true entry functions
            # (no callers among defined functions)
            callers_map: dict[str, list[str]] = defaultdict(list)
            for fn_name in defined_funcs:
                for callee in filtered_call_graph.get(fn_name, set()):
                    callers_map[callee].append(fn_name)

            layer_specs = self._process_layer(
                layer=layer,
                parsed=parsed,
                callers_map=callers_map,
                all_specs=all_specs,
                is_entry_layer=is_entry_layer,
                domain_knowledge=domain_knowledge,
                cross_file_caller_contexts=cross_file_caller_contexts,
            )
            all_specs.update(layer_specs)

            # Save specs for this layer
            for fn_name, spec in layer_specs.items():
                if fn_name in defined_funcs:
                    self.store.save_spec(driver_name, fn_name, spec)

        # Ensure every defined function has a spec (fallback for any missed)
        for fn_name in defined_funcs:
            if fn_name not in all_specs:
                logger.warning("Function '%s' has no spec; using fallback", fn_name)
                all_specs[fn_name] = _fallback_spec(fn_name, "not reached in layer ordering")
                self.store.save_spec(driver_name, fn_name, all_specs[fn_name])

        # Attach callee specs to each function's spec
        for fn_name in defined_funcs:
            spec = all_specs[fn_name]
            for callee in parsed.call_graph.get(fn_name, set()):
                if callee in all_specs:
                    spec.callee_specs[callee] = all_specs[callee]

        return {fn: all_specs[fn] for fn in defined_funcs}

    def _build_generation_order(self, call_graph: dict[str, set[str]]) -> list[list[str]]:
        """Return layers: [[entry_funcs], [layer2_funcs], ...]"""
        return _build_generation_order(call_graph)

    # ------------------------------------------------------------------
    # Layer processing
    # ------------------------------------------------------------------

    def _process_layer(
        self,
        layer: list[str],
        parsed: ParsedCFile,
        callers_map: dict[str, list[str]],
        all_specs: dict[str, Spec],
        is_entry_layer: bool,
        domain_knowledge: str,
        cross_file_caller_contexts: Optional[dict] = None,
    ) -> dict[str, Spec]:
        """Generate specs for all functions in a layer, concurrently."""

        def generate_one(fn_name: str) -> tuple[str, Spec]:
            func_info = parsed.get_function_info(fn_name)
            if func_info is None:
                return fn_name, _fallback_spec(fn_name, "function info not found")

            # Lite mode: skip the LLM spec_gen call and emit a permissive
            # spec (pre = post = true). CBMC's built-in checks still surface
            # memory-safety bugs from the harness inputs, and the LLM budget
            # shifts to realism / classifier in Phase 3 where the LLM adds
            # net signal rather than parroting the function body.
            # When ``lite_with_contracts`` is True (default), the spec is
            # further enriched with deterministic universal contracts
            # derived from parameter names (paired-pointer ordering, …) —
            # still no LLM, but enough to suppress the dominant
            # caller-contract-slip FP class on userland libraries.
            if bool(getattr(self.config, "lite_mode", False)):
                with_contracts = bool(getattr(self.config, "lite_with_contracts", True))
                return fn_name, _permissive_spec(
                    fn_name,
                    func_info=func_info,
                    with_contracts=with_contracts,
                    struct_definitions=getattr(parsed, "struct_definitions", None),
                    cbmc_unwind=int(getattr(self.config, "cbmc_unwind", 4)),
                )

            struct_context = self._extract_struct_context(func_info, parsed)

            callers = callers_map.get(fn_name, [])
            # Cross-file callers: (caller FunctionInfo, caller's ParsedCFile)
            # tuples for callers of fn_name that live in OTHER source files.
            # Without these, a function whose only (or only dangerous) callers
            # are in another file is treated as an entry function and gets an
            # over-approximate precondition — the intra-file blind spot that
            # makes e.g. a sink reachable from another file look like an FP.
            xcallers = list((cross_file_caller_contexts or {}).get(fn_name, []))
            if (not callers and not xcallers) or (is_entry_layer and not xcallers):
                if self.config.enable_dual_spec:
                    spec = self._generate_dual_spec(func_info, domain_knowledge, struct_context)
                else:
                    spec = self._generate_entry_spec(func_info, domain_knowledge, struct_context)
            else:
                # Collect expected specs from all callers — intra-file AND cross-file.
                expected: list[Spec] = []
                for caller_name in callers:
                    caller_info = parsed.get_function_info(caller_name)
                    if caller_info is not None:
                        exp = self._generate_expected_spec(caller_info, fn_name)
                        expected.append(exp)
                for xcaller in xcallers:
                    # tuple (FunctionInfo, ParsedCFile) — be tolerant of shape
                    xcaller_info = xcaller[0] if isinstance(xcaller, (tuple, list)) else xcaller
                    if xcaller_info is not None:
                        try:
                            expected.append(self._generate_expected_spec(xcaller_info, fn_name))
                        except Exception as exc:  # cross-file caller parse hiccup
                            logger.debug("cross-file expected-spec for '%s' from '%s' failed: %s",
                                         fn_name, getattr(xcaller_info, "name", "?"), exc)
                spec = self._generate_internal_spec(func_info, expected, domain_knowledge, struct_context)

            spec.status = SpecStatus.GENERATED
            return fn_name, spec

        results: dict[str, Spec] = {}
        max_workers = min(len(layer), self.config.batch_size)
        if max_workers <= 0:
            max_workers = 1

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(generate_one, fn): fn for fn in layer}
            for future in as_completed(futures):
                fn_name = futures[future]
                try:
                    name, spec = future.result()
                    results[name] = spec
                except Exception as exc:
                    logger.error("Unexpected error generating spec for '%s': %s", fn_name, exc)
                    results[fn_name] = _fallback_spec(fn_name, str(exc))

        return results

    # ------------------------------------------------------------------
    # Struct context extraction
    # ------------------------------------------------------------------

    _BASIC_C_TYPES = frozenset({
        "int", "char", "void", "float", "double", "long", "short",
        "unsigned", "signed", "size_t", "ssize_t", "bool",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "int8_t", "int16_t", "int32_t", "int64_t",
        "uintptr_t", "intptr_t", "ptrdiff_t",
    })
    _SKIP_TOKENS = frozenset({
        "const", "volatile", "restrict", "static", "extern",
        "struct", "union", "enum", "inline", "__inline__",
    })

    def _extract_struct_names(self, func: FunctionInfo) -> list[str]:
        """Return struct-like type names used in func's parameters (heuristic)."""
        names: list[str] = []
        for ptype, _ in func.signature.parameters:
            clean = re.sub(r"[*\[\]]", "", ptype)
            for token in clean.split():
                token = token.strip()
                if (token
                        and token not in self._BASIC_C_TYPES
                        and token not in self._SKIP_TOKENS
                        and not token.startswith("__")):
                    names.append(token)
        return list(dict.fromkeys(names))  # deduplicate, preserve order

    def _extract_struct_context(self, func: FunctionInfo, parsed: ParsedCFile) -> str:
        """
        Build a prompt section describing struct definitions and constructors for
        the struct types that appear in func's parameter list.

        Providing this lets the LLM see invariants established by constructors
        (e.g. assert(size < 0x40000000) in stbtt__new_buf) and include them in
        the generated precondition.
        """
        struct_names = self._extract_struct_names(func)
        if not struct_names:
            return ""

        source = parsed.preprocessed_source or ""
        parts: list[str] = []

        for sname in struct_names:
            # --- struct definition ---
            if source:
                m = re.search(
                    r"typedef\s+struct\s*\w*\s*\{[^}]*\}\s*"
                    + re.escape(sname)
                    + r"\s*;",
                    source,
                    re.DOTALL,
                )
                if m:
                    parts.append(f"Struct definition for `{sname}`:\n{m.group(0).strip()}")

            # --- constructor / factory functions ---
            for fn_name, sig in parsed.functions.items():
                if sname not in sig.return_type:
                    continue
                body = parsed.function_bodies.get(fn_name, "")
                if not body:
                    continue
                params_str = ", ".join(
                    f"{pt} {pn}".strip() for pt, pn in sig.parameters
                )
                body_preview = body[:600].rstrip()
                if len(body) > 600:
                    body_preview += "\n    /* ... */"
                parts.append(
                    f"Constructor `{sig.return_type} {fn_name}({params_str})`"
                    f" — note any assert/bounds constraints:\n{body_preview}"
                )

        if not parts:
            return ""

        header = (
            "Struct context (definitions and constructors for types used in this function).\n"
            "Pay attention to any assert() or bounds constraints in constructors — they\n"
            "reflect invariants that always hold on the struct fields:\n\n"
        )
        return header + "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Spec generation helpers
    # ------------------------------------------------------------------

    def _format_signature(self, func: FunctionInfo) -> str:
        """Format function signature as a string."""
        sig = func.signature
        params = ", ".join(
            f"{ptype} {pname}".strip() for ptype, pname in sig.parameters
        )
        return f"{sig.return_type} {sig.name}({params})"

    def _generate_entry_spec(
        self,
        func: FunctionInfo,
        domain_knowledge: str,
        struct_context: str = "",
    ) -> Spec:
        """Generate spec for an entry function using implementation + domain knowledge."""
        logger.debug("Generating entry spec for '%s'", func.name)

        tm = getattr(self.config, "threat_model", "security")
        user_prompt = ENTRY_SPEC_PROMPT.format(
            threat_model_context=THREAT_MODEL_CONTEXT.get(tm, THREAT_MODEL_CONTEXT["security"]),
            domain_knowledge=domain_knowledge or "No additional domain knowledge provided.",
            struct_context=struct_context or "None.",
            signature=self._format_signature(func),
            body=func.body,
        )

        try:
            result = self._complete_with_vacuous_critique(user_prompt, func)
            if result is not None:
                pre, post = result.precondition, result.postcondition
                post = _relax_postcondition_for_error_paths(post, func.body, func.name)
                return Spec(
                    function_name=func.name,
                    precondition=pre,
                    postcondition=post,
                    pre_validity=result.pre_validity,
                    pre_protocol=result.pre_protocol,
                    status=SpecStatus.GENERATED,
                )
            else:
                logger.warning(
                    "Could not parse LLM response for entry spec of '%s'", func.name
                )
                return _fallback_spec(func.name, "unparseable LLM response")
        except LLMError as exc:
            return _fallback_spec(func.name, str(exc))

    def _generate_internal_spec(
        self,
        func: FunctionInfo,
        expected_specs: list[Spec],
        domain_knowledge: str,
        struct_context: str = "",
    ) -> Spec:
        """Generate spec for internal function from caller expected specs + implementation."""
        logger.debug(
            "Generating internal spec for '%s' from %d caller spec(s)",
            func.name,
            len(expected_specs),
        )

        if not expected_specs:
            # No caller information — treat like an entry function
            return self._generate_entry_spec(func, domain_knowledge, struct_context)

        # Format expected specs for the prompt
        expected_text_parts = []
        for i, esp in enumerate(expected_specs, 1):
            expected_text_parts.append(
                f"Caller {i} ({esp.function_name}):\n"
                f"  Expected precondition: {esp.precondition}\n"
                f"  Expected postcondition: {esp.postcondition}"
            )
        expected_text = "\n\n".join(expected_text_parts)

        tm = getattr(self.config, "threat_model", "security")
        user_prompt = INTERNAL_SPEC_PROMPT.format(
            threat_model_context=THREAT_MODEL_CONTEXT.get(tm, THREAT_MODEL_CONTEXT["security"]),
            expected_specs=expected_text,
            signature=self._format_signature(func),
            body=func.body,
            domain_knowledge=domain_knowledge or "No additional domain knowledge provided.",
            struct_context=struct_context or "None.",
        )

        try:
            result = self._complete_with_vacuous_critique(user_prompt, func)
            if result is not None:
                pre, post = result.precondition, result.postcondition
                post = _relax_postcondition_for_error_paths(post, func.body, func.name)
                return Spec(
                    function_name=func.name,
                    precondition=pre,
                    postcondition=post,
                    pre_validity=result.pre_validity,
                    pre_protocol=result.pre_protocol,
                    status=SpecStatus.GENERATED,
                )
            else:
                logger.warning(
                    "Could not parse LLM response for internal spec of '%s'", func.name
                )
                # Fall back to a weak spec rather than reusing expected specs,
                # because expected specs use the caller's variable names (e.g.
                # 'dev') not the callee's parameter names (e.g. 'rb').
                return _fallback_spec(func.name, "parse failure")
        except LLMError as exc:
            return _fallback_spec(func.name, str(exc))

    def _generate_expected_spec(
        self,
        caller: FunctionInfo,
        callee_name: str,
    ) -> Spec:
        """Generate the expected spec for callee_name from caller's perspective."""
        logger.debug(
            "Generating expected spec for '%s' from caller '%s'",
            callee_name,
            caller.name,
        )

        user_prompt = EXPECTED_SPEC_PROMPT.format(
            caller_name=caller.name,
            caller_signature=self._format_signature(caller),
            caller_body=caller.body,
            callee_name=callee_name,
        )

        try:
            response = self.llm.complete(
                self._spec_system_prompt, user_prompt, role="spec_gen",
            )
            result = _parse_llm_spec_response(response, callee_name, simple_specs=getattr(self.config, 'simple_specs', False))
            if result is not None:
                pre, post = result.precondition, result.postcondition
                return Spec(
                    function_name=callee_name,
                    precondition=pre,
                    postcondition=post,
                    pre_validity=result.pre_validity,
                    pre_protocol=result.pre_protocol,
                    status=SpecStatus.PENDING,
                )
            else:
                logger.warning(
                    "Could not parse expected spec response for '%s' from caller '%s'",
                    callee_name,
                    caller.name,
                )
                return Spec(
                    function_name=callee_name,
                    precondition=_FALLBACK_PRECONDITION,
                    postcondition=_FALLBACK_POSTCONDITION,
                    status=SpecStatus.PENDING,
                )
        except LLMError as exc:
            logger.warning(
                "LLM error generating expected spec for '%s' from '%s': %s",
                callee_name,
                caller.name,
                exc,
            )
            return Spec(
                function_name=callee_name,
                precondition=_FALLBACK_PRECONDITION,
                postcondition=_FALLBACK_POSTCONDITION,
                status=SpecStatus.PENDING,
            )

    def _generate_dual_spec(
        self,
        func: "FunctionInfo",
        domain_knowledge: str,
        struct_context: str = "",
        caller_context: str = "",
    ) -> "Spec":
        """
        Generate spec twice with different emphases. If disagreement detected, flag spec.
        Falls back to single-shot generation if dual fails.
        """
        if not self.config.enable_dual_spec:
            # Fall back to standard single generation
            return self._generate_entry_spec(func, domain_knowledge, struct_context)

        sig = self._format_signature(func)

        # Caller-heavy emphasis
        try:
            user_prompt_a = CALLER_HEAVY_SPEC_PROMPT.format(
                signature=sig,
                caller_context=caller_context or "No caller context available.",
                body=func.body,
            )
            response_a = self.llm.complete(
                self._spec_system_prompt, user_prompt_a, role="spec_gen",
            )
            result_a = _parse_llm_spec_response(response_a, func.name, simple_specs=getattr(self.config, 'simple_specs', False))
        except Exception:
            result_a = None

        # Implementation-heavy emphasis
        try:
            user_prompt_b = IMPL_HEAVY_SPEC_PROMPT.format(
                signature=sig,
                body=func.body,
            )
            response_b = self.llm.complete(
                self._spec_system_prompt, user_prompt_b, role="spec_gen",
            )
            result_b = _parse_llm_spec_response(response_b, func.name, simple_specs=getattr(self.config, 'simple_specs', False))
        except Exception:
            result_b = None

        # If both failed, fall back
        if result_a is None and result_b is None:
            return self._generate_entry_spec(func, domain_knowledge)

        # Pick the richer of the two results.
        # Default order (preferred): caller-heavy (a). Only if caller-heavy is
        # vacuous `true`/`true` AND implementation-heavy is non-vacuous do we
        # swap, so a substantive impl-heavy spec doesn't get discarded just
        # because the caller-side prompt returned a default. This is the
        # cheapest spec-ensemble win: dual-spec already issues two LLM calls,
        # we just stop preferring the first one unconditionally.
        def _is_vacuous(r: Optional[ParsedSpec]) -> bool:
            if r is None:
                return True
            pre_s = (r.precondition or "").strip()
            post_s = (r.postcondition or "").strip()
            return pre_s in ("true", "") and post_s in ("true", "")

        if result_a is None:
            chosen = result_b
        elif _is_vacuous(result_a) and not _is_vacuous(result_b):
            logger.debug(
                "Dual-spec: caller-heavy vacuous, using impl-heavy for '%s'",
                func.name,
            )
            chosen = result_b
        else:
            chosen = result_a
        pre, post = chosen.precondition, chosen.postcondition
        post = _relax_postcondition_for_error_paths(post, func.body, func.name)

        # Check disagreement if both succeeded
        disagree = False
        if result_a and result_b:
            try:
                disagree_prompt = SPEC_DISAGREEMENT_PROMPT.format(
                    pre_a=result_a.precondition, post_a=result_a.postcondition,
                    pre_b=result_b.precondition, post_b=result_b.postcondition,
                )
                disagree_response = self.llm.complete(
                    self._spec_system_prompt, disagree_prompt, role="spec_gen",
                )
                import json as _json
                parsed = _json.loads(disagree_response.strip())
                disagree = bool(parsed.get("disagree", False))
                if disagree:
                    logger.warning(
                        "Dual spec disagreement for '%s': %s",
                        func.name, parsed.get("reason", ""),
                    )
            except Exception:
                pass  # disagreement check is best-effort

        return Spec(
            function_name=func.name,
            precondition=pre,
            postcondition=post,
            pre_validity=chosen.pre_validity,
            pre_protocol=chosen.pre_protocol,
            status=SpecStatus.GENERATED,
            spec_disagreement=disagree,
        )
