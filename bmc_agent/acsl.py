"""ACSL/Frama-C pilot support for BMC-Agent specs.

This module is intentionally narrow. It translates the subset of the
BMC-Agent DSL that has a direct ACSL function-contract interpretation,
injects those contracts into C sources, optionally recovers plain C
``assert(...)`` calls into ACSL statement assertions, and runs Frama-C/WP.

It is not a replacement backend for the existing CBMC/Kani pipeline yet.
The goal is to make the ACSL direction testable without changing the
default verification semantics.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from bmc_agent.dsl_to_cbmc import _match_call, _strip_outer_parens, _top_level_split
from bmc_agent.parser import FunctionSignature, ParsedCFile
from bmc_agent.spec import Spec


_LABEL_RE = re.compile(
    r"^(?:requires?|ensures?|precondition:|postcondition:)\s*",
    re.IGNORECASE,
)
_BARE_RESULT_RE = re.compile(r"(?<![\w.])(?<!->)result\b")
_NULL_LITERAL_RE = re.compile(r"\b(?:NULL|null)\b(?!\s*\()")
_ACSL_BUILTIN_RE = re.compile(r"\\[A-Za-z_]\w*")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_TYPE_TAG_PREFIX_RE = re.compile(r"\b(?:struct|union|enum)\s+[A-Za-z_]\w*")
_BARE_IDENT_RE = re.compile(r"(?<![\w.])(?<!->)([A-Za-z_]\w*)")
_FRAMA_PROVED_RE = re.compile(r"\[wp\]\s+Proved goals:\s+(\d+)\s*/\s*(\d+)")


_ALWAYS_BOUND_IDENTIFIERS = frozenset(
    {
        "sizeof",
        "true",
        "false",
        "int",
        "long",
        "short",
        "char",
        "unsigned",
        "signed",
        "void",
        "const",
        "volatile",
        "static",
        "extern",
        "inline",
        "restrict",
        "bool",
        "size_t",
        "ssize_t",
        "ptrdiff_t",
        "uintptr_t",
        "intptr_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
    }
)


@dataclass
class AcslClause:
    """One translated contract clause."""

    kind: str
    source: str
    expr: str | None
    reason: str = ""

    @property
    def translated(self) -> bool:
        return self.expr is not None


@dataclass
class AcslContract:
    """Rendered ACSL function contract plus translation diagnostics."""

    function_name: str
    text: str
    clauses: list[AcslClause] = field(default_factory=list)
    unsupported: list[AcslClause] = field(default_factory=list)
    loop_invariants_unsupported: list[str] = field(default_factory=list)

    @property
    def translated_clause_count(self) -> int:
        return sum(1 for c in self.clauses if c.translated)


@dataclass
class AcslSourceBuild:
    """Result of recovering assertions and injecting contracts."""

    source_text: str
    contracts: dict[str, AcslContract]
    inserted_functions: list[str]
    skipped_functions: dict[str, str]
    recovered_asserts: int = 0


@dataclass
class FramaCResult:
    """Compact result from a Frama-C/WP invocation."""

    command: list[str]
    returncode: int | None
    runtime_s: float
    stdout: str
    stderr: str
    timed_out: bool = False
    proved_goals: int | None = None
    total_goals: int | None = None

    @property
    def status(self) -> str:
        if self.timed_out:
            return "timeout"
        output = (self.stdout + "\n" + self.stderr).lower()
        if self.returncode not in (0, None) and (
            "annot-error" in output
            or "invalid user input" in output
            or "syntax error" in output
        ):
            return "annotation_error"
        if self.returncode not in (0, None):
            return "error"
        if self.proved_goals is not None and self.total_goals is not None:
            if self.total_goals == 0:
                return "success"
            return "success" if self.proved_goals == self.total_goals else "unproved"
        return "success" if self.returncode == 0 else "error"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "returncode": self.returncode,
            "runtime_s": self.runtime_s,
            "command": self.command,
            "timed_out": self.timed_out,
            "proved_goals": self.proved_goals,
            "total_goals": self.total_goals,
        }


def load_specs_json(path: str | Path) -> dict[str, Spec]:
    """Load one or more Spec objects from JSON.

    Accepted shapes:
      * a single Spec dict with ``function_name``;
      * a mapping of function name to Spec dict;
      * a list of Spec dicts.
    """

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "function_name" in data:
        spec = Spec.from_dict(data)
        return {spec.function_name: spec}
    if isinstance(data, list):
        specs: dict[str, Spec] = {}
        for item in data:
            if not isinstance(item, dict) or "function_name" not in item:
                raise ValueError(f"Unsupported spec item in {path}: {item!r}")
            spec = Spec.from_dict(item)
            specs[spec.function_name] = spec
        return specs
    if isinstance(data, dict):
        specs = {}
        for name, item in data.items():
            if not isinstance(item, dict):
                raise ValueError(f"Unsupported spec item for {name!r} in {path}")
            if "function_name" not in item:
                item = {"function_name": name, **item}
            spec = Spec.from_dict(item)
            specs[spec.function_name] = spec
        return specs
    raise ValueError(f"Unsupported spec JSON shape in {path}")


def translate_spec_to_acsl(
    spec: Spec,
    signature: FunctionSignature,
    *,
    add_assigns_nothing: bool = False,
) -> AcslContract:
    """Translate one BMC-Agent Spec into an ACSL function contract."""

    param_names = [name for _, name in signature.parameters if name]
    pre_validity, pre_protocol = spec.split_precondition()
    pre_parts = [p for p in (pre_validity, pre_protocol) if p and p.strip()]
    if not pre_parts and spec.precondition.strip():
        pre_parts = [spec.precondition]

    clauses: list[AcslClause] = []
    for part in pre_parts:
        clauses.extend(
            translate_condition_to_acsl(
                part,
                kind="requires",
                param_names=param_names,
                allow_result=False,
            )
        )
    clauses.extend(
        translate_condition_to_acsl(
            spec.postcondition,
            kind="ensures",
            param_names=param_names,
            allow_result=True,
        )
    )

    unsupported = [c for c in clauses if not c.translated]
    rendered: list[str] = []
    for clause in clauses:
        if clause.expr is not None:
            rendered.append(f"  {clause.kind} {clause.expr};")
    if add_assigns_nothing:
        rendered.append("  assigns \\nothing;")

    if not rendered:
        return AcslContract(
            function_name=spec.function_name,
            text="",
            clauses=clauses,
            unsupported=unsupported,
            loop_invariants_unsupported=list(spec.loop_invariants),
        )

    text = "/*@\n" + "\n".join(rendered) + "\n*/"
    return AcslContract(
        function_name=spec.function_name,
        text=text,
        clauses=clauses,
        unsupported=unsupported,
        loop_invariants_unsupported=list(spec.loop_invariants),
    )


def translate_condition_to_acsl(
    condition: str,
    *,
    kind: str,
    param_names: Sequence[str],
    allow_result: bool,
) -> list[AcslClause]:
    """Translate a pre/postcondition string into ACSL clauses."""

    if not condition or condition.strip().lower() in {"true", "1", "\\true"}:
        return []

    stripped = _LABEL_RE.sub("", condition.strip())
    coarse_parts = re.split(r"\s+AND\s+|\n|;", stripped)
    clauses: list[AcslClause] = []
    for coarse in coarse_parts:
        coarse = coarse.strip()
        if not coarse:
            continue
        # Split plain top-level conjunctions into separate ACSL clauses.
        # If an unparenthesized top-level disjunction is present, preserve
        # the whole expression so C/ACSL operator precedence is not changed.
        has_top_or = len(_top_level_split(coarse, "||")) > 1
        candidates = _top_level_split(coarse, "&&") if not has_top_or else [coarse]
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            translated = _translate_expr(
                candidate,
                param_names=set(param_names),
                allow_result=allow_result,
            )
            clauses.append(AcslClause(kind=kind, source=candidate, **translated))
    return clauses


def _translate_expr(
    expr: str,
    *,
    param_names: set[str],
    allow_result: bool,
) -> dict[str, str | None]:
    expr = _strip_outer_parens(expr.strip())
    if not expr:
        return {"expr": None, "reason": "empty clause"}
    if expr.lower() in {"true", "1", "\\true"}:
        return {"expr": "\\true", "reason": ""}
    if expr.lower() in {"false", "0", "\\false"}:
        return {"expr": "\\false", "reason": ""}
    if _contains_unsupported_logic(expr):
        return {"expr": None, "reason": "unsupported quantifier/implication"}

    # Preserve C/ACSL precedence: split top-level OR first, then AND inside
    # each disjunct.
    parts_or = _top_level_split(expr, "||")
    if len(parts_or) > 1:
        translated: list[str] = []
        reasons: list[str] = []
        for part in parts_or:
            inner = _translate_expr(
                part,
                param_names=param_names,
                allow_result=allow_result,
            )
            if inner["expr"] is None:
                reasons.append(str(inner["reason"]))
            else:
                translated.append(f"({inner['expr']})")
        if reasons:
            return {"expr": None, "reason": "; ".join(r for r in reasons if r)}
        return {"expr": " || ".join(translated), "reason": ""}

    parts_and = _top_level_split(expr, "&&")
    if len(parts_and) > 1:
        translated = []
        reasons = []
        for part in parts_and:
            inner = _translate_expr(
                part,
                param_names=param_names,
                allow_result=allow_result,
            )
            if inner["expr"] is None:
                reasons.append(str(inner["reason"]))
            else:
                translated.append(f"({inner['expr']})")
        if reasons:
            return {"expr": None, "reason": "; ".join(r for r in reasons if r)}
        return {"expr": " && ".join(translated), "reason": ""}

    if expr.startswith("!") and not expr.startswith("!="):
        inner = _translate_expr(
            expr[1:].strip(),
            param_names=param_names,
            allow_result=allow_result,
        )
        if inner["expr"] is None:
            return inner
        return {"expr": f"!({inner['expr']})", "reason": ""}

    call = _whole_call(expr, "valid_string")
    if call is not None and len(call) >= 1:
        return _check_bound(f"\\valid_read_string({call[0]})", param_names, allow_result)

    call = _whole_call(expr, "valid_range")
    if call is not None:
        if len(call) != 3:
            return {"expr": None, "reason": "valid_range expects 3 arguments"}
        ptr, lo, hi = call
        acsl = (
            f"{ptr} != \\null && {lo} >= 0 && {hi} >= {lo} && "
            f"({hi} == {lo} || \\valid({ptr} + ({lo} .. {hi} - 1)))"
        )
        return _check_bound(acsl, param_names, allow_result)

    call = _whole_call(expr, "valid")
    if call is not None and len(call) >= 1:
        return _check_bound(f"\\valid({call[0]})", param_names, allow_result)

    call = _whole_call(expr, "owns")
    if call is not None and len(call) >= 1:
        # ACSL has no direct ownership primitive in this pilot. We keep the
        # memory-validity part because it is the checkable safety obligation.
        return _check_bound(f"\\valid({call[-1]})", param_names, allow_result)

    call = _whole_call(expr, "null")
    if call is not None and len(call) >= 1:
        return _check_bound(f"{call[0]} == \\null", param_names, allow_result)

    call = _whole_call(expr, "in_bounds")
    if call is not None:
        if len(call) != 2:
            return {"expr": None, "reason": "in_bounds expects 2 arguments"}
        arr, idx = call
        return _check_bound(
            f"{idx} >= 0 && {idx} < (int)(sizeof({arr})/sizeof({arr}[0]))",
            param_names,
            allow_result,
        )

    for unsupported in ("locked", "no_overflow", "valid_user_pointer"):
        if _whole_call(expr, unsupported) is not None:
            return {"expr": None, "reason": f"unsupported DSL primitive: {unsupported}"}

    bare = _normalize_bare_acsl_expr(expr, allow_result=allow_result)
    if not _looks_like_formula(bare):
        return {"expr": None, "reason": "not a pure C/ACSL formula"}
    return _check_bound(bare, param_names, allow_result)


def _whole_call(expr: str, name: str) -> list[str] | None:
    match = _match_call(expr, name)
    if match is None:
        return None
    start, end, args = match
    if expr[:start].strip() or expr[end:].strip():
        return None
    return args


def _normalize_bare_acsl_expr(expr: str, *, allow_result: bool) -> str:
    expr = expr.strip()
    expr = _NULL_LITERAL_RE.sub(r"\\null", expr)
    if allow_result:
        expr = expr.replace("\\result", "__ACSL_RESULT__")
        expr = _BARE_RESULT_RE.sub(r"\\result", expr)
        expr = expr.replace("__ACSL_RESULT__", "\\result")
    return expr


def _contains_unsupported_logic(expr: str) -> bool:
    return bool(re.search(r"\b(forall|exists)\b|==>|<==>", expr, re.IGNORECASE))


def _looks_like_formula(expr: str) -> bool:
    if not expr:
        return False
    prose = re.compile(
        r"\b(the|from|into|with|where|when|after|before|between|contains|"
        r"increased|decreased|written|removed|bytes|buffer|value|values|"
        r"call|called|returned|means|that|which|have|has|been|must|should|"
        r"will|all|each|every|any|otherwise)\b",
        re.IGNORECASE,
    )
    if prose.search(expr):
        return False
    if re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}\s+[A-Za-z]{2,}", expr):
        return False
    return bool(
        re.search(r"==|!=|<=|>=|<|>|\|\||&&|!|\\valid|\\null|\\result|\\true|\\false", expr)
    )


def _check_bound(expr: str, param_names: set[str], allow_result: bool) -> dict[str, str | None]:
    unbound = _find_unbound_identifier(expr, param_names, allow_result=allow_result)
    if unbound:
        return {"expr": None, "reason": f"unbound identifier: {unbound}"}
    return {"expr": expr, "reason": ""}


def _find_unbound_identifier(
    expr: str,
    param_names: set[str],
    *,
    allow_result: bool,
) -> str | None:
    scan = expr
    if allow_result:
        scan = scan.replace("\\result", "result")
    scan = _ACSL_BUILTIN_RE.sub("", scan)
    scan = _STRING_RE.sub("", scan)
    scan = re.sub(r"/\*.*?\*/", "", scan, flags=re.DOTALL)
    scan = _TYPE_TAG_PREFIX_RE.sub("", scan)
    bound = set(param_names)
    if allow_result:
        bound.add("result")
    for match in _BARE_IDENT_RE.finditer(scan):
        ident = match.group(1)
        if ident in bound or ident in _ALWAYS_BOUND_IDENTIFIERS:
            continue
        if ident.startswith("_"):
            continue
        return ident
    return None


def recover_plain_asserts_to_acsl(source_text: str) -> tuple[str, int]:
    """Replace executable ``assert(EXPR);`` statements with ACSL assertions.

    The scanner skips comments and string/character literals, so a comment
    mentioning ``assert(x)`` is left unchanged.
    """

    out: list[str] = []
    i = 0
    count = 0
    state = "normal"
    while i < len(source_text):
        ch = source_text[i]
        nxt = source_text[i : i + 2]

        if state == "line_comment":
            out.append(ch)
            if ch == "\n":
                state = "normal"
            i += 1
            continue
        if state == "block_comment":
            out.append(ch)
            if nxt == "*/":
                out.append(source_text[i + 1])
                i += 2
                state = "normal"
            else:
                i += 1
            continue
        if state == "string":
            out.append(ch)
            if ch == "\\" and i + 1 < len(source_text):
                out.append(source_text[i + 1])
                i += 2
                continue
            if ch == '"':
                state = "normal"
            i += 1
            continue
        if state == "char":
            out.append(ch)
            if ch == "\\" and i + 1 < len(source_text):
                out.append(source_text[i + 1])
                i += 2
                continue
            if ch == "'":
                state = "normal"
            i += 1
            continue

        if nxt == "//":
            out.append(nxt)
            i += 2
            state = "line_comment"
            continue
        if nxt == "/*":
            out.append(nxt)
            i += 2
            state = "block_comment"
            continue
        if ch == '"':
            out.append(ch)
            i += 1
            state = "string"
            continue
        if ch == "'":
            out.append(ch)
            i += 1
            state = "char"
            continue

        if _starts_assert_call(source_text, i):
            found = _read_assert_statement(source_text, i)
            if found is not None:
                end, expr = found
                out.append(f"//@ assert {expr.strip()};")
                i = end
                count += 1
                continue

        out.append(ch)
        i += 1

    return "".join(out), count


def _starts_assert_call(text: str, idx: int) -> bool:
    if not text.startswith("assert", idx):
        return False
    before = text[idx - 1] if idx > 0 else ""
    after_idx = idx + len("assert")
    after = text[after_idx] if after_idx < len(text) else ""
    if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
        return False
    j = after_idx
    while j < len(text) and text[j].isspace():
        j += 1
    return j < len(text) and text[j] == "("


def _read_assert_statement(text: str, idx: int) -> tuple[int, str] | None:
    j = idx + len("assert")
    while j < len(text) and text[j].isspace():
        j += 1
    if j >= len(text) or text[j] != "(":
        return None
    start = j + 1
    depth = 1
    j += 1
    state = "normal"
    while j < len(text):
        ch = text[j]
        if state == "string":
            if ch == "\\":
                j += 2
                continue
            if ch == '"':
                state = "normal"
            j += 1
            continue
        if state == "char":
            if ch == "\\":
                j += 2
                continue
            if ch == "'":
                state = "normal"
            j += 1
            continue
        if ch == '"':
            state = "string"
            j += 1
            continue
        if ch == "'":
            state = "char"
            j += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                expr = text[start:j]
                j += 1
                while j < len(text) and text[j].isspace():
                    j += 1
                if j < len(text) and text[j] == ";":
                    return j + 1, expr
                return None
        j += 1
    return None


def build_acsl_source(
    source_text: str,
    parsed: ParsedCFile,
    specs: Mapping[str, Spec],
    *,
    recover_asserts: bool = False,
    add_assigns_nothing: bool = False,
    functions: Iterable[str] | None = None,
) -> AcslSourceBuild:
    """Recover statement assertions and inject ACSL contracts."""

    working = source_text
    recovered = 0
    if recover_asserts:
        working, recovered = recover_plain_asserts_to_acsl(working)

    selected = set(functions or specs.keys())
    contracts: dict[str, AcslContract] = {}
    for name in selected:
        spec = specs.get(name)
        sig = parsed.functions.get(name)
        if spec is None or sig is None:
            continue
        contract = translate_spec_to_acsl(
            spec,
            sig,
            add_assigns_nothing=add_assigns_nothing,
        )
        contracts[name] = contract

    insertions: list[tuple[int, str, str]] = []
    skipped: dict[str, str] = {}
    for name, contract in contracts.items():
        if not contract.text:
            skipped[name] = "no translated contract clauses"
            continue
        definition = parsed.function_definitions.get(name, "")
        if not definition:
            skipped[name] = "function definition unavailable"
            continue
        idx = working.find(definition)
        if idx < 0:
            idx = _find_function_definition_start(working, name)
        if idx < 0:
            skipped[name] = "function definition text not found"
            continue
        insertions.append((idx, name, contract.text))

    for idx, _name, text in sorted(insertions, key=lambda x: x[0], reverse=True):
        working = working[:idx] + text + "\n" + working[idx:]

    inserted = [name for _idx, name, _text in sorted(insertions, key=lambda x: x[0])]
    return AcslSourceBuild(
        source_text=working,
        contracts=contracts,
        inserted_functions=inserted,
        skipped_functions=skipped,
        recovered_asserts=recovered,
    )


def _find_function_definition_start(source_text: str, function_name: str) -> int:
    """Best-effort fallback when prior source recovery changed function bodies."""

    pattern = re.compile(
        rf"(?m)^[A-Za-z_][\w\s\*\(\),\[\]]*?\b{re.escape(function_name)}\s*"
        rf"\([^;{{}}]*\)\s*\{{"
    )
    match = pattern.search(source_text)
    return match.start() if match else -1


def run_frama_c_wp(
    source_path: str | Path,
    *,
    wp_timeout: int = 30,
    command: str = "",
    docker_image: str = "framac/frama-c:26.0.debian",
    timeout: int = 120,
    cpus: float = 4.0,
    extra_args: Sequence[str] = (),
) -> FramaCResult:
    """Run Frama-C/WP on an annotated C file."""

    source = Path(source_path).resolve()
    if command:
        cmd = shlex.split(command) + [
            "-wp",
            "-wp-prover",
            "z3",
            "-wp-timeout",
            str(wp_timeout),
            *extra_args,
            str(source),
        ]
    else:
        mount = _choose_mount_root(source)
        rel = source.relative_to(mount)
        cmd = [
            "docker",
            "run",
            "--rm",
            "--cpus",
            str(cpus),
            "-v",
            f"{mount}:/work",
            "-w",
            "/work",
            docker_image,
            "frama-c",
            "-wp",
            "-wp-prover",
            "z3",
            "-wp-timeout",
            str(wp_timeout),
            *extra_args,
            str(rel),
        ]

    env = os.environ.copy()
    if not command:
        env.setdefault("DOCKER_HOST", "unix:///var/run/docker.sock")

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        runtime = time.monotonic() - start
        stdout = proc.stdout
        stderr = proc.stderr
        proved, total = _parse_frama_goals(stdout + "\n" + stderr)
        return FramaCResult(
            command=cmd,
            returncode=proc.returncode,
            runtime_s=runtime,
            stdout=stdout,
            stderr=stderr,
            proved_goals=proved,
            total_goals=total,
        )
    except subprocess.TimeoutExpired as exc:
        runtime = time.monotonic() - start
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        return FramaCResult(
            command=cmd,
            returncode=None,
            runtime_s=runtime,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )


def _choose_mount_root(path: Path) -> Path:
    # Mount a stable project/workspace ancestor so relative includes still work
    # for generated files under artifacts.
    for parent in [path.parent, *path.parents]:
        if (parent / "pyproject.toml").exists() or parent.name == "jw_bmc":
            return parent
    return path.parent


def _parse_frama_goals(output: str) -> tuple[int | None, int | None]:
    matches = list(_FRAMA_PROVED_RE.finditer(output))
    if not matches:
        return None, None
    proved, total = matches[-1].groups()
    return int(proved), int(total)


def acsl_build_report(build: AcslSourceBuild) -> dict:
    """Return JSON-serializable diagnostics for an ACSL source build."""

    contracts = {}
    for name, contract in build.contracts.items():
        contracts[name] = {
            "translated_clause_count": contract.translated_clause_count,
            "unsupported": [
                {
                    "kind": c.kind,
                    "source": c.source,
                    "reason": c.reason,
                }
                for c in contract.unsupported
            ],
            "loop_invariants_unsupported": contract.loop_invariants_unsupported,
            "inserted": name in build.inserted_functions,
            "skip_reason": build.skipped_functions.get(name, ""),
        }
    return {
        "recovered_asserts": build.recovered_asserts,
        "inserted_functions": build.inserted_functions,
        "skipped_functions": build.skipped_functions,
        "contracts": contracts,
    }
