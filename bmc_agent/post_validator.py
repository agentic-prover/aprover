"""Post-sweep revalidation of bmc-agent findings.

Applies four mechanical checks to each finding, independent of the LLM judge:

  1. crash_site:   the top libarchive stack frame in the sanitizer output must
                   equal the function under test (or one of its static callees).
  2. sanitizer_class: the sanitizer error class must match the CBMC property
                     class (pointer_dereference → SEGV/heap-or-stack-buffer-overflow,
                     overflow → runtime-overflow / allocation-size-too-big, etc.).
                     LSan-only signals never confirm a memory-corruption property.
  3. antipattern: the reproducer source must not contain known bad patterns
                  (archive_write_open_memory(... &X, &X) aliasing,
                  missing free() of archive_entry_acl_to_text return, ...).
  4. fallback:    if dyn-val didn't trigger anything, the finding stays a
                  candidate (no confirmation either way).

Produces a revised verdict per finding. Does NOT call the LLM, does NOT re-run
CBMC. Safe to apply to already-completed sweep output as an A/B against the
existing labels.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------- sanitizer parsing ------------------------------------------------

# Matches an ASan/UBSan/LSan frame line of the form:
#   #N 0xADDR in FUNC /path/to/file.c:LINE
# We don't require the trailing :line — some frames omit it (e.g. _start).
_FRAME_RX = re.compile(
    r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>[\w.:<>~]+)\s+(?P<path>/[^\s:]+)"
)

# Sanitizer ERROR-line patterns we recognise. We tag each by family.
_SAN_PATTERNS = [
    ("heap-buffer-overflow",   re.compile(r"AddressSanitizer:\s*heap-buffer-overflow")),
    ("stack-buffer-overflow",  re.compile(r"AddressSanitizer:\s*stack-buffer-overflow")),
    ("global-buffer-overflow", re.compile(r"AddressSanitizer:\s*global-buffer-overflow")),
    ("use-after-free",         re.compile(r"AddressSanitizer:\s*heap-use-after-free")),
    ("alloc-too-big",          re.compile(r"AddressSanitizer:\s*requested allocation size .* exceeds maximum")),
    ("SEGV",                   re.compile(r"AddressSanitizer:\s*SEGV")),
    ("null-deref",             re.compile(r"runtime error:.*null pointer", re.I)),
    ("signed-overflow",        re.compile(r"runtime error:.*signed integer overflow", re.I)),
    ("unsigned-overflow",      re.compile(r"runtime error:.*unsigned integer overflow", re.I)),
    ("oob-load",               re.compile(r"runtime error:.*load of address.*outside")),
    ("lsan-leak",              re.compile(r"LeakSanitizer:\s*detected memory leaks", re.I)),
]

# Map CBMC failing-property class → set of sanitizer signals that count as
# a same-class confirmation. LSan never confirms a memory-corruption claim.
_CLASS_TABLE: dict[str, set[str]] = {
    "pointer_dereference": {
        "SEGV", "null-deref",
        "heap-buffer-overflow", "stack-buffer-overflow", "global-buffer-overflow",
        "use-after-free", "oob-load",
    },
    "overflow": {
        "signed-overflow", "unsigned-overflow", "alloc-too-big",
    },
    "array_bounds": {
        "heap-buffer-overflow", "stack-buffer-overflow", "global-buffer-overflow",
        "oob-load",
    },
    "unwind": set(),   # CBMC unwinding-assertion failures aren't bugs
}


@dataclass
class SanitizerInfo:
    family: Optional[str]            # one of _SAN_PATTERNS keys, or None
    top_libarchive_frame: Optional[tuple[str, str]]  # (func, path) or None
    has_lsan_leak: bool
    has_real_crash: bool             # non-LSan sanitizer fired


def parse_sanitizer_output(stderr: str) -> SanitizerInfo:
    """Pull the salient facts out of an ASan/UBSan/LSan stderr blob."""
    if not stderr:
        return SanitizerInfo(family=None, top_libarchive_frame=None,
                             has_lsan_leak=False, has_real_crash=False)

    # Find the dominant (first) error class.
    family = None
    for name, pat in _SAN_PATTERNS:
        if pat.search(stderr):
            family = name
            break

    has_lsan_leak = bool(_SAN_PATTERNS[-1][1].search(stderr))
    has_real_crash = family is not None and family != "lsan-leak"

    # Find the top frame whose path is in libarchive/libarchive/ (not build dir,
    # not sanitizer runtime, not libc).
    top_lib_frame = None
    for line in stderr.splitlines():
        m = _FRAME_RX.match(line)
        if not m:
            continue
        path = m.group("path")
        if "/libarchive/libarchive/" not in path:
            continue
        # Skip the sanitizer runtime / libc / harness's own main()
        top_lib_frame = (m.group("func"), path)
        break

    return SanitizerInfo(
        family=family,
        top_libarchive_frame=top_lib_frame,
        has_lsan_leak=has_lsan_leak,
        has_real_crash=has_real_crash,
    )


# ---------- antipattern lint --------------------------------------------------

# (name, regex, description) — keep tight; only catalog antipatterns we've
# actually observed producing FPs in real sweeps.
_ANTIPATTERNS: list[tuple[str, re.Pattern, str]] = [
    (
        "write_open_memory_size_aliasing",
        # archive_write_open_memory(..., &X, ..., &X) where the same address
        # is passed for the buffSize arg (3rd) and used arg (4th).
        # Capture &NAME then look for another &NAME inside the same call.
        re.compile(
            r"archive_write_open_memory\s*\([^)]*?&(\w+)\s*,\s*&\1\s*\)",
            re.DOTALL,
        ),
        "archive_write_open_memory called with the same address aliased for "
        "buffSize and used — corrupts caller's stack (3rd arg is size_t value, "
        "not pointer; 4th arg is size_t*)",
    ),
    (
        "write_open_memory_size_by_pointer",
        # archive_write_open_memory(arch, BUF, &SIZE, ...) — size_t value
        # expected as 3rd arg, not its address.
        re.compile(
            r"archive_write_open_memory\s*\(\s*[^,]+,\s*[^,]+,\s*&\w+\s*,",
            re.DOTALL,
        ),
        "archive_write_open_memory's 3rd arg must be size_t value, not &size_t — "
        "passing the address corrupts the size that memory_write reads",
    ),
]


# archive_entry_acl_to_text returns a malloc'd buffer. The leak check is
# context-sensitive — a single regex can't tell "freed elsewhere in the file"
# from "freed in the same statement", so it lives in its own predicate below.
_ACL_TO_TEXT_CALL_RX = re.compile(r"\barchive_entry_acl_to_text\s*\(")
# Assignment capturing the return value into a variable: `... *t = acl_to_text(...)`.
_ACL_TO_TEXT_ASSIGN_RX = re.compile(
    r"(\w+)\s*=\s*archive_entry_acl_to_text\s*\("
)
# `free(` followed by an identifier (to allow binding to a captured variable).
_FREE_CALL_OF_RX_TMPL = r"\bfree\s*\(\s*{var}\s*\)"


def _acl_to_text_leak_hits(src: str) -> list[AntipatternHit]:
    """Custom predicate: archive_entry_acl_to_text without matching free.

    Heuristic, intentionally simple:
      * If there's an inline `free(archive_entry_acl_to_text(...))`, clean.
      * If the return is captured into a variable and `free(<var>)` appears
        anywhere in the file, clean.
      * If the return is captured but never freed by name, leak.
      * If the call appears with no capture (return discarded or used inline)
        AND no `free(` of any kind appears in the file, leak.
    """
    calls = list(_ACL_TO_TEXT_CALL_RX.finditer(src))
    if not calls:
        return []

    has_any_free = bool(re.search(r"\bfree\s*\(", src))
    # Inline `free(archive_entry_acl_to_text(...))` — accept if every call
    # is wrapped this way. Detect by checking the 6 chars before the call.
    def _is_inline_freed(m: re.Match) -> bool:
        start = max(0, m.start() - 8)
        return "free(" in src[start:m.start()]

    captured_vars = {m.group(1) for m in _ACL_TO_TEXT_ASSIGN_RX.finditer(src)}
    unfreed_vars = {
        v for v in captured_vars
        if not re.search(_FREE_CALL_OF_RX_TMPL.format(var=re.escape(v)), src)
    }

    if unfreed_vars:
        return [AntipatternHit(
            name="acl_to_text_leak",
            description=(
                "archive_entry_acl_to_text return assigned to "
                f"{sorted(unfreed_vars)} but never freed — produces a "
                "LeakSanitizer signal unrelated to the CBMC claim"
            ),
            match_text=f"unfreed vars: {sorted(unfreed_vars)}",
        )]

    # No capture variants: only flag if the file has no free() at all.
    uncaptured = [m for m in calls if not _is_inline_freed(m)
                  and not any(m.group(0) in line
                              for line in src.splitlines()
                              if "=" in line and "archive_entry_acl_to_text" in line)]
    if uncaptured and not has_any_free:
        return [AntipatternHit(
            name="acl_to_text_leak",
            description=(
                "archive_entry_acl_to_text called with no free() anywhere in "
                "the reproducer — produces a LeakSanitizer signal unrelated "
                "to the CBMC claim"
            ),
            match_text=uncaptured[0].group(0),
        )]
    return []


@dataclass
class AntipatternHit:
    name: str
    description: str
    match_text: str  # the matched code snippet (truncated)


def lint_reproducer(reproducer_path: Path) -> list[AntipatternHit]:
    """Return any antipattern matches in the reproducer source."""
    if not reproducer_path.exists():
        return []
    try:
        src = reproducer_path.read_text(errors="replace")
    except OSError:
        return []
    hits: list[AntipatternHit] = []
    for name, pat, desc in _ANTIPATTERNS:
        m = pat.search(src)
        if m:
            hits.append(AntipatternHit(
                name=name,
                description=desc,
                match_text=m.group(0)[:200],
            ))
    hits.extend(_acl_to_text_leak_hits(src))
    return hits


# ---------- property-class extraction ----------------------------------------

def extract_property_class(failing_property: str) -> str:
    """From e.g. 'strcmp.pointer_dereference.1' → 'pointer_dereference'.
       From 'archive_acl_text_len.overflow.3' → 'overflow'.
       From 'main.unwind.0' → 'unwind'.
    """
    if not failing_property:
        return ""
    parts = failing_property.split(".")
    # Property class is typically the second-to-last segment, but the
    # leading function name can itself contain dots. Walk right-to-left:
    # last segment is the index (digit), preceding segments form the class.
    for i in range(len(parts) - 1, -1, -1):
        if not parts[i].isdigit():
            return parts[i]
    return ""


# ---------- revalidation -----------------------------------------------------

@dataclass
class RevalidationResult:
    finding_id: str                  # function + property
    original_verdict: str            # judge.verdict (realistic/unrealistic/uncertain)
    original_dyn_outcome: str        # primary_dynamic_validation.outcome
    revised_label: str               # confirmed_clean / candidate / fp_*
    reasons: list[str] = field(default_factory=list)
    sanitizer_family: Optional[str] = None
    top_libarchive_frame_func: Optional[str] = None
    antipatterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "original_verdict": self.original_verdict,
            "original_dyn_outcome": self.original_dyn_outcome,
            "revised_label": self.revised_label,
            "reasons": self.reasons,
            "sanitizer_family": self.sanitizer_family,
            "top_libarchive_frame_func": self.top_libarchive_frame_func,
            "antipatterns": self.antipatterns,
        }


def revalidate_judge_json(
    judge_json_path: Path,
    target_function: str,
    static_callees: Optional[set[str]] = None,
) -> RevalidationResult:
    """Revalidate a single judge_<property>.json file.

    target_function: the function under CBMC test (the parent directory name
                     in the v7 layout).
    static_callees:  optional set of function names statically reachable from
                     target_function — if the top libarchive frame is one of
                     these, crash-site still counts as a match. None = no
                     callee info; require exact match on target_function.
    """
    data = json.loads(judge_json_path.read_text())
    failing_property = data.get("failing_property", "")
    judge = data.get("judge") or {}
    dyn = data.get("primary_dynamic_validation") or {}

    finding_id = f"{target_function}/{failing_property}"
    original_verdict = str(judge.get("verdict", ""))
    original_dyn_outcome = str(dyn.get("outcome", ""))

    result = RevalidationResult(
        finding_id=finding_id,
        original_verdict=original_verdict,
        original_dyn_outcome=original_dyn_outcome,
        revised_label="candidate",
    )

    prop_class = extract_property_class(failing_property)

    # No dyn-val run / didn't trigger / harness errored — leave as candidate.
    if not dyn or original_dyn_outcome in ("", "not_triggered", "timeout", "llm_no_reproducer", "build_failed"):
        result.reasons.append(f"dyn-val outcome={original_dyn_outcome or 'absent'}; no execution evidence")
        return result

    # ---- 1. parse sanitizer ----
    san_info = parse_sanitizer_output(dyn.get("stderr") or dyn.get("stderr_excerpt") or "")
    result.sanitizer_family = san_info.family
    if san_info.top_libarchive_frame:
        result.top_libarchive_frame_func = san_info.top_libarchive_frame[0]

    # ---- 2. sanitizer-class check ----
    allowed = _CLASS_TABLE.get(prop_class, set())
    if san_info.has_lsan_leak and not san_info.has_real_crash:
        result.revised_label = "fp_leak_only"
        result.reasons.append(
            f"only LeakSanitizer signal — does not confirm '{prop_class}' property"
        )
        return result

    if not san_info.has_real_crash:
        result.revised_label = "candidate"
        result.reasons.append("no sanitizer crash detected in stderr — judge tagged "
                              f"'{original_dyn_outcome}' but no recognised ASan/UBSan signal")
        return result

    if san_info.family and allowed and san_info.family not in allowed:
        result.revised_label = "fp_wrong_sanitizer_class"
        result.reasons.append(
            f"sanitizer family '{san_info.family}' does not match CBMC property "
            f"class '{prop_class}' (allowed: {sorted(allowed)})"
        )
        return result

    # ---- 3. crash-site check ----
    if san_info.top_libarchive_frame is None:
        result.revised_label = "fp_no_libarchive_frame"
        result.reasons.append(
            "no libarchive frame in sanitizer stack — crash is in sanitizer runtime, "
            "libc, or reproducer's own main() — not a libarchive bug"
        )
        return result

    crash_func = san_info.top_libarchive_frame[0]
    valid_funcs = {target_function}
    if static_callees:
        valid_funcs |= static_callees
    if crash_func not in valid_funcs:
        result.revised_label = "fp_wrong_crash_site"
        result.reasons.append(
            f"top libarchive frame is '{crash_func}' (in {san_info.top_libarchive_frame[1]}) "
            f"but bug claim is in '{target_function}' — crash is elsewhere in libarchive"
        )
        return result

    # ---- 4. antipattern lint ----
    repro_path_str = dyn.get("harness_path") or ""
    if repro_path_str:
        hits = lint_reproducer(Path(repro_path_str))
        if hits:
            result.antipatterns = [h.name for h in hits]
            result.revised_label = "fp_reproducer_antipattern"
            result.reasons.append(
                "reproducer contains known bad pattern(s): " +
                "; ".join(f"{h.name} — {h.description}" for h in hits)
            )
            return result

    # All four checks passed — this is a clean dynamic confirmation.
    result.revised_label = "confirmed_clean"
    result.reasons.append(
        f"sanitizer={san_info.family}, top libarchive frame={crash_func}, "
        f"matches CBMC '{prop_class}' class, no antipatterns"
    )
    return result


def revalidate_finding_dir(
    finding_dir: Path,
    static_callees: Optional[set[str]] = None,
) -> list[RevalidationResult]:
    """Revalidate every judge_<property>.json under a single function dir.

    The function name is taken from the directory name (v7 layout:
    <output>/judge_v7/<module>/<function>/judge_<property>.json).
    """
    target_function = finding_dir.name
    out: list[RevalidationResult] = []
    for jp in sorted(finding_dir.glob("judge_*.json")):
        try:
            out.append(revalidate_judge_json(jp, target_function, static_callees))
        except Exception as exc:
            out.append(RevalidationResult(
                finding_id=f"{target_function}/{jp.stem}",
                original_verdict="",
                original_dyn_outcome="",
                revised_label="revalidate_error",
                reasons=[f"exception during revalidation: {exc!r}"],
            ))
    return out


def revalidate_sweep_output(
    sweep_root: Path,
) -> list[RevalidationResult]:
    """Revalidate every finding under a complete sweep output tree.

    Layout: <sweep_root>/<module>/<function>/judge_<property>.json
    """
    results: list[RevalidationResult] = []
    for module_dir in sorted(p for p in sweep_root.iterdir() if p.is_dir()):
        for fn_dir in sorted(p for p in module_dir.iterdir() if p.is_dir()):
            results.extend(revalidate_finding_dir(fn_dir))
    return results


# ---------- CLI --------------------------------------------------------------

# Labels whose mere presence in the output is a demotion from the LLM judge's
# verdict (anything starting with "fp_" plus "revalidate_error").
_DEMOTION_LABELS = {
    "fp_leak_only",
    "fp_wrong_sanitizer_class",
    "fp_no_libarchive_frame",
    "fp_wrong_crash_site",
    "fp_reproducer_antipattern",
    "revalidate_error",
}


def _format_text_report(results: list[RevalidationResult]) -> str:
    """Human-readable report grouped by revised_label."""
    if not results:
        return "(no findings)\n"
    by_label: dict[str, list[RevalidationResult]] = {}
    for r in results:
        by_label.setdefault(r.revised_label, []).append(r)

    lines: list[str] = []
    # Show demotions first, then candidate/confirmed_clean.
    label_order = (
        sorted(l for l in by_label if l in _DEMOTION_LABELS)
        + sorted(l for l in by_label if l not in _DEMOTION_LABELS)
    )
    for label in label_order:
        bucket = by_label[label]
        lines.append(f"\n=== {label} ({len(bucket)}) ===")
        for r in bucket:
            lines.append(
                f"  {r.finding_id}"
                f"  [judge:{r.original_verdict or '?'}, "
                f"dyn:{r.original_dyn_outcome or '?'}]"
            )
            if r.sanitizer_family:
                lines.append(f"    sanitizer={r.sanitizer_family}"
                             + (f", top_frame={r.top_libarchive_frame_func}"
                                if r.top_libarchive_frame_func else ""))
            if r.antipatterns:
                lines.append(f"    antipatterns={r.antipatterns}")
            for reason in r.reasons:
                lines.append(f"    - {reason}")

    # Tally line.
    n = len(results)
    n_demoted = sum(1 for r in results if r.revised_label in _DEMOTION_LABELS)
    n_clean = sum(1 for r in results if r.revised_label == "confirmed_clean")
    n_candidate = sum(1 for r in results if r.revised_label == "candidate")
    lines.append("")
    lines.append(
        f"Summary: {n} finding(s) — "
        f"{n_clean} confirmed_clean, "
        f"{n_candidate} candidate, "
        f"{n_demoted} demoted"
    )

    # Verdict-flip table: judge said realistic but we demoted.
    flips = [
        r for r in results
        if r.original_verdict == "realistic" and r.revised_label in _DEMOTION_LABELS
    ]
    if flips:
        lines.append("")
        lines.append(f"Verdict flips (judge=realistic → demoted): {len(flips)}")
        for r in flips:
            lines.append(f"  {r.finding_id} → {r.revised_label}")
    return "\n".join(lines) + "\n"


def _main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m bmc_agent.post_validator",
        description=("Post-sweep mechanical revalidation of bmc-agent judge "
                     "verdicts. Produces revised labels per finding without "
                     "re-running CBMC or the LLM."),
    )
    parser.add_argument(
        "sweep_root",
        type=Path,
        help=("Sweep output root. Layout: "
              "<sweep_root>/<module>/<function>/judge_<property>.json"),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable report.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write report to this path instead of stdout.",
    )
    parser.add_argument(
        "--only-flips",
        action="store_true",
        help=("Exit non-zero (and only print flips) if any judge=realistic "
              "finding got demoted. Useful for CI / smoke checks."),
    )
    args = parser.parse_args()

    if not args.sweep_root.is_dir():
        print(f"error: not a directory: {args.sweep_root}", file=sys.stderr)
        return 2

    results = revalidate_sweep_output(args.sweep_root)

    flips = [
        r for r in results
        if r.original_verdict == "realistic" and r.revised_label in _DEMOTION_LABELS
    ]

    if args.json:
        payload = {
            "sweep_root": str(args.sweep_root),
            "n_findings": len(results),
            "n_flips": len(flips),
            "results": [r.to_dict() for r in results],
        }
        text = json.dumps(payload, indent=2)
    elif args.only_flips:
        if not flips:
            text = "No verdict flips (judge=realistic & demoted).\n"
        else:
            lines = [f"Verdict flips ({len(flips)}):"]
            for r in flips:
                lines.append(f"  {r.finding_id} → {r.revised_label}")
                for reason in r.reasons:
                    lines.append(f"    - {reason}")
            text = "\n".join(lines) + "\n"
    else:
        text = _format_text_report(results)

    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)

    if args.only_flips and flips:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
