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
from typing import Optional

from bmc_agent.artifacts import ArtifactStore
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient, LLMError
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile, parse_c_file
from bmc_agent.prompts import (
    CALLER_HEAVY_SPEC_PROMPT,
    DSL_GRAMMAR,
    ENTRY_SPEC_PROMPT,
    EXPECTED_SPEC_PROMPT,
    IMPL_HEAVY_SPEC_PROMPT,
    INTERNAL_SPEC_PROMPT,
    SPEC_DISAGREEMENT_PROMPT,
    SPEC_SYSTEM_PROMPT,
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


def _parse_llm_spec_response(response: str, func_name: str) -> Optional[tuple[str, str]]:
    """
    Parse LLM JSON response into (precondition, postcondition).

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

    try:
        data = json.loads(text)
        pre = data.get("precondition", "").strip()
        post = data.get("postcondition", "").strip()
        if pre and post:
            return pre, post
    except (json.JSONDecodeError, AttributeError):
        pass

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_specs(
        self,
        source_file: str,
        driver_name: str,
        domain_knowledge: str = "",
        source_text: Optional[str] = None,
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
        parsed = parse_c_file(source_file, source_text=source_text)

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
    ) -> dict[str, Spec]:
        """Generate specs for all functions in a layer, concurrently."""

        def generate_one(fn_name: str) -> tuple[str, Spec]:
            func_info = parsed.get_function_info(fn_name)
            if func_info is None:
                return fn_name, _fallback_spec(fn_name, "function info not found")

            struct_context = self._extract_struct_context(func_info, parsed)

            callers = callers_map.get(fn_name, [])
            if not callers or is_entry_layer:
                if self.config.enable_dual_spec:
                    spec = self._generate_dual_spec(func_info, domain_knowledge, struct_context)
                else:
                    spec = self._generate_entry_spec(func_info, domain_knowledge, struct_context)
            else:
                # Collect expected specs from all callers
                expected: list[Spec] = []
                for caller_name in callers:
                    caller_info = parsed.get_function_info(caller_name)
                    if caller_info is not None:
                        exp = self._generate_expected_spec(caller_info, fn_name)
                        expected.append(exp)
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

        user_prompt = ENTRY_SPEC_PROMPT.format(
            domain_knowledge=domain_knowledge or "No additional domain knowledge provided.",
            struct_context=struct_context or "None.",
            signature=self._format_signature(func),
            body=func.body,
        )

        try:
            response = self.llm.complete(SPEC_SYSTEM_PROMPT, user_prompt)
            result = _parse_llm_spec_response(response, func.name)
            if result is not None:
                pre, post = result
                return Spec(
                    function_name=func.name,
                    precondition=pre,
                    postcondition=post,
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

        user_prompt = INTERNAL_SPEC_PROMPT.format(
            expected_specs=expected_text,
            signature=self._format_signature(func),
            body=func.body,
            domain_knowledge=domain_knowledge or "No additional domain knowledge provided.",
            struct_context=struct_context or "None.",
        )

        try:
            response = self.llm.complete(SPEC_SYSTEM_PROMPT, user_prompt)
            result = _parse_llm_spec_response(response, func.name)
            if result is not None:
                pre, post = result
                return Spec(
                    function_name=func.name,
                    precondition=pre,
                    postcondition=post,
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
            response = self.llm.complete(SPEC_SYSTEM_PROMPT, user_prompt)
            result = _parse_llm_spec_response(response, callee_name)
            if result is not None:
                pre, post = result
                return Spec(
                    function_name=callee_name,
                    precondition=pre,
                    postcondition=post,
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
            response_a = self.llm.complete(SPEC_SYSTEM_PROMPT, user_prompt_a)
            result_a = _parse_llm_spec_response(response_a, func.name)
        except Exception:
            result_a = None

        # Implementation-heavy emphasis
        try:
            user_prompt_b = IMPL_HEAVY_SPEC_PROMPT.format(
                signature=sig,
                body=func.body,
            )
            response_b = self.llm.complete(SPEC_SYSTEM_PROMPT, user_prompt_b)
            result_b = _parse_llm_spec_response(response_b, func.name)
        except Exception:
            result_b = None

        # If both failed, fall back
        if result_a is None and result_b is None:
            return self._generate_entry_spec(func, domain_knowledge)

        # Use whichever succeeded; prefer caller-heavy
        pre, post = result_a or result_b

        # Check disagreement if both succeeded
        disagree = False
        if result_a and result_b:
            try:
                disagree_prompt = SPEC_DISAGREEMENT_PROMPT.format(
                    pre_a=result_a[0], post_a=result_a[1],
                    pre_b=result_b[0], post_b=result_b[1],
                )
                disagree_response = self.llm.complete(SPEC_SYSTEM_PROMPT, disagree_prompt)
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
            status=SpecStatus.GENERATED,
            spec_disagreement=disagree,
        )
