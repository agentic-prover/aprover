"""Caller-grounded spec generator (v2).

The v1 SpecGenerator drafts each function's spec from a single source —
the function body — which produces tautological specs that bake in
whatever the implementation tolerates (see findings/methodology_insight_
2026-05-22.md for the canonical ncdev_bar_read failure mode).

v2 reconciles three independent evidence sources before drafting:
  1. Function body
  2. K observed call sites (caller-grounded)
  3. Doc annotations + universal-pattern seeds

Each clause carries provenance tags. Boundary functions (declared in
public headers) bypass caller-grounding and get trivial specs, because
their "caller" is attacker-controlled input not a constraint to ground
against. Bottom-up topological ordering ensures callees have real specs
before callers draft.

Interface matches v1's SpecGenerator.generate_specs so pipeline.py can
swap by changing the constructor call. Falls back to v1 behavior for
configurations v2 doesn't yet support (lite_mode, Rust, Kani backend).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from bmc_agent.spec import Spec, SpecStatus
from bmc_agent.prompts import (
    render_caller_grounded_spec_prompt,
    spec_system_prompt_for,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bmc_agent.boundary_detector import BoundaryDetector
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    from bmc_agent.parser import FunctionInfo, ParsedCFile
    from bmc_agent.artifacts import ArtifactStore


# ---------- defaults ---------------------------------------------------------

DEFAULT_K_CALLERS = 5
DEFAULT_CONTEXT_RADIUS = 8
MAX_PARSE_RETRIES = 1   # one extra retry on JSON parse failure


# ---------- structured-output parser -----------------------------------------

# The prompt asks for a single JSON object. LLMs sometimes wrap it in a
# ```json code fence or prepend a leading sentence. We extract the
# outermost {...} block.

_JSON_BLOCK_RX = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first top-level JSON object out of ``text``.

    Robust to ``` fences and stray prose. Returns None on parse failure.
    """
    if not text:
        return None
    # Strip code-fence markers first.
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```", "", cleaned)
    m = _JSON_BLOCK_RX.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # Try a tightening pass: trim from each end until it parses.
        candidate = m.group(0)
        # Try to find a balanced { ... } by walking forward.
        depth = 0
        start = candidate.find("{")
        for i, ch in enumerate(candidate[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None


def _validate_and_extract(
    payload: dict,
    fn_name: str,
) -> Optional[tuple[list[dict], list[dict], list[dict], list[str], bool, str]]:
    """Validate the parsed JSON against the schema in the prompt.

    Returns (pre_validity, pre_protocol, postcondition, loop_invariants,
    spec_disagreement, uncertainty_notes), or None if invalid. Each
    clause dict must have 'clause' and 'evidence' (list of tags).
    """
    if not isinstance(payload, dict):
        logger.warning("spec-gen v2 [%s]: output not a JSON object", fn_name)
        return None

    def _check_clause_list(key: str) -> Optional[list[dict]]:
        raw = payload.get(key, [])
        if not isinstance(raw, list):
            logger.warning("spec-gen v2 [%s]: %s is not a list", fn_name, key)
            return None
        out: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "spec-gen v2 [%s]: %s[%d] is not an object", fn_name, key, i
                )
                return None
            clause = item.get("clause")
            evidence = item.get("evidence")
            if not isinstance(clause, str) or not clause.strip():
                logger.warning(
                    "spec-gen v2 [%s]: %s[%d] missing/empty clause", fn_name, key, i
                )
                return None
            if not isinstance(evidence, list) or not evidence:
                # Rule 1: every clause must have ≥1 evidence tag.
                logger.warning(
                    "spec-gen v2 [%s]: %s[%d] missing evidence tags (clause: %r)",
                    fn_name, key, i, clause,
                )
                return None
            if not all(isinstance(t, str) for t in evidence):
                logger.warning(
                    "spec-gen v2 [%s]: %s[%d] non-string evidence tag", fn_name, key, i
                )
                return None
            out.append({"clause": clause.strip(), "evidence": evidence})
        return out

    pv = _check_clause_list("pre_validity")
    pp = _check_clause_list("pre_protocol")
    post = _check_clause_list("postcondition")
    if pv is None or pp is None or post is None:
        return None

    loops_raw = payload.get("loop_invariants", [])
    if not isinstance(loops_raw, list) or not all(isinstance(x, str) for x in loops_raw):
        loops_raw = []

    disagreement = bool(payload.get("spec_disagreement", False))
    notes = str(payload.get("uncertainty_notes", "") or "")

    return pv, pp, post, loops_raw, disagreement, notes


def _build_spec_from_validated(
    fn_name: str,
    pv: list[dict],
    pp: list[dict],
    post: list[dict],
    loops: list[str],
    disagreement: bool,
    status: SpecStatus = SpecStatus.GENERATED,
) -> Spec:
    """Assemble a Spec from validated structured clauses."""
    pre_validity_str = " && ".join(c["clause"] for c in pv) if pv else ""
    pre_protocol_str = " && ".join(c["clause"] for c in pp) if pp else ""
    # Composite precondition for v1-compatibility consumers.
    combined_pre_parts = [s for s in (pre_validity_str, pre_protocol_str) if s]
    precondition = " && ".join(combined_pre_parts) if combined_pre_parts else "true"
    postcondition = " && ".join(c["clause"] for c in post) if post else "true"

    evidence: dict[str, list[str]] = {}
    for c in pv + pp + post:
        evidence[c["clause"]] = list(c["evidence"])

    return Spec(
        function_name=fn_name,
        precondition=precondition,
        postcondition=postcondition,
        loop_invariants=list(loops),
        status=status,
        spec_disagreement=disagreement,
        pre_validity=pre_validity_str,
        pre_protocol=pre_protocol_str,
        evidence=evidence,
    )


# ---------- helpers: fallback specs -----------------------------------------


def _trivial_spec(fn_name: str, evidence_tag: str, reason: str = "") -> Spec:
    """Permissive `true/true` spec with a single evidence tag.

    Used for: boundary functions (tag=external_boundary), and as the
    last-resort fallback when LLM parse fails twice (tag=failed_parse).
    """
    return Spec(
        function_name=fn_name,
        precondition="true",
        postcondition="true",
        status=SpecStatus.GENERATED if evidence_tag == "external_boundary" else SpecStatus.FAILED,
        evidence={"true": [evidence_tag]} if evidence_tag else {},
    )


# ---------- body-evidence: handle-magic-check PRE inference -----------------
#
# Almost every public C library API starts with a "handle validation"
# check that derefs its first argument:
#
#   archive_check_magic(_a, ARCHIVE_MATCH_MAGIC, ...);   // libarchive
#   __archive_check_magic((_a), ((0xcad11c9U)), ...);   // preprocessed form
#
# The macro IS the canonical encoding of the public-API caller contract:
# "I require a valid handle of type X." Treating the boundary spec as
# ``true/true`` (the alternative) makes BMC explore _a == NULL, which
# trivially crashes inside the check and produces a caller-contract-slip
# CEx on every single public API in the project — the dominant FP shape
# we saw on libarchive.
#
# This helper does a body-level regex match for the magic-check pattern
# and, when found, returns the contract clauses (param != NULL AND
# param->magic == M) to use as the PRE instead.

_MAGIC_CHECK_RE = re.compile(
    r"\b"
    r"(?P<macro>\w*[Cc]heck[_]?[Mm]agic\w*)"
    r"\s*\(\s*"
    r"\(*\s*(?P<param>[A-Za-z_]\w*)\s*\)*\s*,"  # 1st arg, optional parens
    r"\s*"
    r"\(*\s*(?P<magic>[\w0-9xX]+[uUlL]*)\s*\)*"  # 2nd arg
)


def _looks_like_magic_constant(token: str) -> bool:
    """A handle-validation 2nd argument is meaningful only if it looks
    like a magic constant — either an UPPERCASE identifier containing
    MAGIC, or an integer/hex literal. Filters out cases where the regex
    accidentally matches some other call (a flag enum, etc.)."""
    t = token.strip().rstrip("uUlL")
    if not t:
        return False
    if t.startswith(("0x", "0X")):
        return all(c in "0123456789abcdefABCDEFxX" for c in t)
    if t.isdigit():
        return True
    # Symbolic form: must be UPPERCASE and contain "MAGIC"
    return t.isupper() and "MAGIC" in t


def _infer_handle_contract_precondition(
    func_info: "FunctionInfo",
) -> Optional[tuple[str, str]]:
    """Scan the function body for a handle-validation magic-check call
    on a parameter, and return ``(param_name, magic_token)`` when found.

    Returns None when no such pattern is found — the caller should fall
    back to the trivial PRE.

    Examples that match:
      archive_check_magic(_a, ARCHIVE_MATCH_MAGIC, ...);
      __archive_check_magic((_a), ((0xcad11c9U)), ...);

    Examples that do NOT match:
      foo(x, 0)           — 2nd arg isn't magic-shaped
      check(x)            — name doesn't contain magic
      bar(local, MAGIC)   — 1st arg isn't a parameter
    """
    body = getattr(func_info, "body", None) or ""
    if not body:
        return None
    sig = getattr(func_info, "signature", None)
    if sig is None:
        return None
    param_names = {pname for _, pname in sig.parameters if pname}
    if not param_names:
        return None

    # Scan only the first ~10 lines — magic check is always at the top.
    head = "\n".join(body.splitlines()[:12])
    for m in _MAGIC_CHECK_RE.finditer(head):
        param = m.group("param")
        magic = m.group("magic")
        if param not in param_names:
            continue
        if not _looks_like_magic_constant(magic):
            continue
        return (param, magic)
    return None


def _spec_from_handle_contract(
    fn_name: str, param: str, magic: str,
) -> Spec:
    """Build a spec encoding the inferred handle-validation contract."""
    pre = f"{param} != NULL && {param}->magic == {magic}"
    return Spec(
        function_name=fn_name,
        precondition=pre,
        postcondition="true",
        status=SpecStatus.GENERATED,
        pre_validity=pre,
        pre_protocol="",
        evidence={pre: ["caller_contract:magic_check"]},
    )


def _spec_from_seed_only(
    fn_name: str,
    seed_clauses: list,
    reason: str,
) -> Spec:
    """When the LLM fails, fall back to seed-only PRE with no POST.

    Seed clauses are deterministic (universal_contracts patterns) and
    sound; this gives the verification pipeline *something* to work with
    without injecting unsupported guesses.
    """
    clause_texts = [s.clause for s in seed_clauses if s.clause]
    pre = " && ".join(clause_texts) if clause_texts else "true"
    evidence = {c: ["signature_pattern"] for c in clause_texts}
    return Spec(
        function_name=fn_name,
        precondition=pre,
        postcondition="true",
        status=SpecStatus.FAILED,
        pre_validity=pre,
        pre_protocol="",
        evidence=evidence,
    )


# ---------- the orchestrator ------------------------------------------------


class SpecGeneratorV2:
    """Caller-grounded spec generator. Drop-in for v1 SpecGenerator.

    Constructor takes the same (config, llm, store) plus two v2-specific
    optional kwargs: ``boundary_detector`` and ``corpus_paths``. When
    omitted, behaves like a single-file v1 (no caller-grounding, no
    boundary skipping) — strictly worse than passing them but
    operational.
    """

    def __init__(
        self,
        config: "Config",
        llm: "LLMClient",
        store: "ArtifactStore",
        *,
        boundary_detector: Optional["BoundaryDetector"] = None,
        corpus_paths: Optional[list[Path]] = None,
        k_callers: int = DEFAULT_K_CALLERS,
    ) -> None:
        self.config = config
        self.llm = llm
        self.store = store
        self.boundary_detector = boundary_detector
        self.corpus_paths = list(corpus_paths) if corpus_paths else []
        self.k_callers = k_callers
        self._spec_system_prompt: str = spec_system_prompt_for("c")

    # -- public interface (matches v1) ---------------------------------------

    def generate_specs(
        self,
        source_file: str,
        driver_name: str,
        domain_knowledge: str = "",
        source_text: Optional[str] = None,
    ) -> dict[str, Spec]:
        """Generate specs for every function defined in ``source_file``.

        Output shape: same as v1 — dict mapping function name → Spec.
        Each Spec's ``evidence`` field is populated with per-clause
        provenance tags.
        """
        from bmc_agent.source_parser import parse_source_file as _parse  # type: ignore

        logger.info("v2 spec-gen parsing %s", source_file)
        parsed = _parse(source_file, source_text=source_text)

        # Trim header-inlined helpers if this is a preprocessed TU.
        primary = getattr(parsed, "primary_source", None)
        if primary and hasattr(parsed, "restrict_to_primary_source"):
            kept = len(parsed.functions)
            dropped = parsed.restrict_to_primary_source()
            if dropped:
                logger.info(
                    "v2: preprocessed TU; kept %d, dropped %d header helpers",
                    kept - dropped, dropped,
                )

        # Configure system prompt.
        from bmc_agent.source_parser import detect_language
        language = detect_language(source_file)
        strict = bool(getattr(self.config, "strict_dsl", False))
        safety_only = bool(getattr(self.config, "safety_only", False))
        self._spec_system_prompt = spec_system_prompt_for(
            language, strict=strict, safety_only=safety_only,
        )

        self.store.init_driver(driver_name)

        defined_funcs = set(parsed.functions.keys())

        # Filtered call graph: callees defined in this TU only.
        filtered_call_graph: dict[str, set[str]] = {}
        for fn_name in defined_funcs:
            raw_callees = parsed.call_graph.get(fn_name, set())
            filtered_call_graph[fn_name] = raw_callees & defined_funcs

        # Bottom-up layers: leaves first.
        layers = _build_bottom_up_layers(filtered_call_graph)
        logger.info("v2: bottom-up layers: %s", layers)

        # Stub specs for external callees (matches v1).
        all_specs: dict[str, Spec] = {}
        for fn_name in defined_funcs:
            for callee in parsed.call_graph.get(fn_name, set()):
                if callee not in defined_funcs and callee not in all_specs:
                    all_specs[callee] = _trivial_spec(callee, "external_stub")

        # Corpus paths default: just the source file under spec.
        corpus = self.corpus_paths or [Path(source_file)]

        # Process layer by layer (leaves → roots).
        for layer_idx, layer in enumerate(layers):
            logger.info("v2: layer %d/%d: %s", layer_idx + 1, len(layers), layer)
            for fn_name in layer:
                func_info = parsed.get_function_info(fn_name)
                if func_info is None:
                    all_specs[fn_name] = _trivial_spec(fn_name, "missing_info")
                    continue
                spec = self._generate_one(
                    func_info=func_info,
                    parsed=parsed,
                    all_specs_so_far=all_specs,
                    corpus_paths=corpus,
                )
                all_specs[fn_name] = spec
                if fn_name in defined_funcs:
                    self.store.save_spec(driver_name, fn_name, spec)

        # Attach callee specs (matches v1 finalisation).
        for fn_name in defined_funcs:
            spec = all_specs.get(fn_name)
            if spec is None:
                spec = _trivial_spec(fn_name, "not_reached")
                all_specs[fn_name] = spec
            for callee in parsed.call_graph.get(fn_name, set()):
                if callee in all_specs:
                    spec.callee_specs[callee] = all_specs[callee]

        return {fn: all_specs[fn] for fn in defined_funcs}

    # -- per-function flow ---------------------------------------------------

    def _generate_one(
        self,
        *,
        func_info: "FunctionInfo",
        parsed: "ParsedCFile",
        all_specs_so_far: dict[str, Spec],
        corpus_paths: list[Path],
    ) -> Spec:
        """The full 7-step flow for one function (see module docstring).

        Order matters — earlier steps cheap-path past the LLM call.
        """
        fn_name = func_info.name

        # Step 1: canonical short-circuit (free, authoritative).
        try:
            from bmc_agent.universal_stub_contracts import canonical_signature
            if canonical_signature(fn_name) is not None:
                logger.debug("v2 [%s]: canonical_contract short-circuit", fn_name)
                return Spec(
                    function_name=fn_name,
                    precondition="true",
                    postcondition="true",
                    status=SpecStatus.GENERATED,
                    evidence={"true": ["canonical_contract"]},
                )
        except ImportError:
            pass

        # Step 2: boundary check → trivial spec (attacker-controlled input).
        # Before falling back to ``true/true``, try to infer a handle-
        # validation contract from the body — many C libraries (libarchive,
        # sqlite3, libcurl) start every public API with a magic-check macro
        # that documents the caller contract. When present, a permissive
        # boundary spec lets BMC explore ``handle == NULL`` and generates
        # caller-contract-slip CExes on every public API — the dominant FP
        # shape on libarchive. Encoding the handle contract at spec time
        # prevents that whole FP class before BMC runs.
        if self.boundary_detector and self.boundary_detector.is_boundary(fn_name):
            contract = _infer_handle_contract_precondition(func_info)
            if contract is not None:
                param, magic = contract
                logger.info(
                    "v2 [%s]: boundary function — inferred handle "
                    "contract: %s != NULL && %s->magic == %s",
                    fn_name, param, param, magic,
                )
                return _spec_from_handle_contract(fn_name, param, magic)
            logger.debug("v2 [%s]: boundary function — trivial spec", fn_name)
            return _trivial_spec(fn_name, "external_boundary")

        # Step 3: gather evidence.
        from bmc_agent.spec_evidence import gather_evidence_bundle
        bundle = gather_evidence_bundle(
            func_info=func_info,
            parsed_file=parsed,
            corpus_paths=corpus_paths,
            k_callers=self.k_callers,
            struct_definitions=getattr(parsed, "struct_definitions", None),
            cbmc_unwind=int(getattr(self.config, "cbmc_unwind", 4)),
            candidate_fn_names=set(parsed.functions.keys()),
        )

        # Step 4: render prompt + LLM call.
        callee_specs_dict = {
            callee: all_specs_so_far[callee].to_dict()
            for callee in parsed.call_graph.get(fn_name, set())
            if callee in all_specs_so_far
        }
        sig = func_info.signature
        params_str = ", ".join(f"{t} {n}" for t, n in sig.parameters) or "void"
        fn_sig_str = f"{sig.return_type} {sig.name}({params_str})"
        prompt = render_caller_grounded_spec_prompt(
            fn_signature=fn_sig_str,
            fn_body=func_info.body,
            callers=bundle.callers,
            address_taken_sites=bundle.address_taken_sites,
            doc_annotations=bundle.doc_annotations,
            seed_clauses=bundle.seed_clauses,
            field_accesses=bundle.field_accesses,
            callee_specs=callee_specs_dict,
            n_callers_actual=self.k_callers,
        )

        # Step 5: delegate to SpecGenAgent for the LLM-call boundary.
        # The agent owns the retry-on-parse-fail loop (max_retries =
        # MAX_PARSE_RETRIES). Validation + Spec construction happen
        # inside its parse(); failure surfaces as result.ok=False.
        from bmc_agent.agents.spec_gen import SpecGenAgent
        agent = SpecGenAgent(
            config=self.config, llm=self.llm,
            system_prompt=self._spec_system_prompt,
        )
        result = agent.run(prompt=prompt, fn_name=fn_name)
        if result.ok:
            spec = result.output
            disagreement = bool(spec.spec_disagreement)
            if disagreement:
                logger.info(
                    "v2 [%s]: spec_disagreement=true", fn_name,
                )

            # Step 5b: v2.2 tool-use branch. Trigger when:
            #   * config.enable_spec_gen_tools is True, AND
            #   * the base spec flagged disagreement (body vs callers
            #     contradicted) OR there's no caller evidence at all
            #     (vtable-only / orphan functions where caller-grounding
            #     fell back to seed-only).
            # The tool-use call gets to fetch additional callers / look
            # up callees / inspect struct fields mid-reasoning, then
            # emits a refined spec. Soundness: output is CBMC-verified
            # downstream, same as the base v2 spec.
            if (
                getattr(self.config, "enable_spec_gen_tools", False)
                and (disagreement or (not bundle.callers
                                       and not bundle.address_taken_sites))
            ):
                refined = self._generate_with_tools(
                    func_info=func_info, parsed=parsed,
                    base_spec=spec, prompt=prompt,
                    bundle=bundle, corpus_paths=corpus_paths,
                    all_specs_so_far=all_specs_so_far,
                )
                if refined is not None:
                    return refined
                # Tool-use failed / declined → fall back to base spec.
            return spec

        # Step 6+7: fall back to seed-only spec.
        logger.warning(
            "v2 [%s]: LLM/parse failed after %d attempts — falling back to seed-only",
            fn_name, MAX_PARSE_RETRIES + 1,
        )
        return _spec_from_seed_only(fn_name, bundle.seed_clauses,
                                    reason="llm_parse_failed")

    # -- v2.2 tool-use branch ------------------------------------------------

    def _generate_with_tools(
        self,
        *,
        func_info: "FunctionInfo",
        parsed: "ParsedCFile",
        base_spec: Spec,
        prompt: str,
        bundle,
        corpus_paths: list[Path],
        all_specs_so_far: dict[str, Spec],
    ) -> Optional[Spec]:
        """v2.2 tool-use branch. Returns a refined Spec when the LLM
        emits one + it passes validation; None when the LLM declined
        or produced something unparseable (caller falls back to the
        base v2 spec).
        """
        from bmc_agent.spec_gen_tools import (
            SpecToolContext, build_spec_gen_tools, TOOL_USE_PROMPT_ADDENDUM,
        )
        ctx = SpecToolContext(
            parsed=parsed,
            corpus_paths=corpus_paths,
            all_specs_so_far=all_specs_so_far,
            boundary_detector=self.boundary_detector,
        )
        tools, handlers = build_spec_gen_tools(ctx)
        fn_name = func_info.name

        # Reuse the same caller-grounded prompt; append the tool-use
        # instructions so the LLM knows it can fetch more data.
        augmented_prompt = prompt + TOOL_USE_PROMPT_ADDENDUM

        try:
            tu_result = self.llm.complete_with_tools(
                system_prompt=self._spec_system_prompt,
                user_prompt=augmented_prompt,
                tools=tools,
                tool_handlers=handlers,
                max_iterations=8,
                max_tool_calls=5,
                max_tokens_per_turn=4096,
                role="spec_gen",
            )
        except Exception as exc:
            logger.warning(
                "v2.2 [%s]: tool-use call failed (%r); using base spec",
                fn_name, exc,
            )
            return None

        if tu_result.error:
            logger.info(
                "v2.2 [%s]: tool-use terminated with error '%s' "
                "(iterations=%d, tool_calls=%d) — using base spec",
                fn_name, tu_result.error,
                tu_result.iterations, tu_result.tool_calls_made,
            )
            return None

        payload = _extract_json_object(tu_result.text)
        if payload is None:
            logger.warning(
                "v2.2 [%s]: tool-use response JSON extract failed — using base spec",
                fn_name,
            )
            return None
        validated = _validate_and_extract(payload, fn_name)
        if validated is None:
            return None
        pv, pp, post, loops, disagreement, notes = validated
        refined = _build_spec_from_validated(
            fn_name, pv, pp, post, loops, disagreement,
            status=SpecStatus.GENERATED,
        )
        logger.info(
            "v2.2 [%s]: tool-use refined spec accepted "
            "(tool_calls=%d, iterations=%d, disagreement=%s)",
            fn_name, tu_result.tool_calls_made,
            tu_result.iterations, disagreement,
        )
        return refined


# ---------- topological layering --------------------------------------------


def _build_bottom_up_layers(call_graph: dict[str, set[str]]) -> list[list[str]]:
    """Return layers: [[leaves], [layer2], ...] (callees before callers).

    Uses Kahn's-like topo sort. Cycles get collapsed: any function still
    in the graph when no leaves remain is emitted as a 'cycle layer' so
    the orchestrator processes it; mutual-recursion callees see stub
    specs on their first encounter, which the next sweep can refine.
    """
    in_degree: dict[str, int] = {fn: 0 for fn in call_graph}
    reverse: dict[str, set[str]] = {fn: set() for fn in call_graph}
    for fn, callees in call_graph.items():
        for c in callees:
            if c in in_degree:
                in_degree[fn] += 1
                reverse.setdefault(c, set()).add(fn)
    # Leaves = functions with no in-graph callees.
    leaves = [fn for fn, deg in in_degree.items() if deg == 0]
    layers: list[list[str]] = []
    remaining = dict(in_degree)
    while leaves:
        layers.append(sorted(leaves))
        next_leaves: list[str] = []
        for fn in leaves:
            del remaining[fn]
            for caller in reverse.get(fn, ()):
                if caller in remaining:
                    remaining[caller] -= 1
                    if remaining[caller] == 0:
                        next_leaves.append(caller)
        leaves = next_leaves
    if remaining:
        # Cycle-breaking: emit everything else as one final layer.
        layers.append(sorted(remaining.keys()))
    return layers
