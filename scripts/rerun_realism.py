"""
Re-run the realism check on bug_report.json files whose original realism
LLM call failed (typically due to the Anthropic workspace API cap).
Uses whatever LLM routing is configured in the environment.

Usage:
    python scripts/rerun_realism.py \
        --sweep /tmp/libarchive_n3_full_out/seedhunt_n3 \
        --corpus /tmp/libarchive_seedhunt_full \
        --log /tmp/rerun_realism.log
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

# Make bmc_agent importable when the script runs from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bmc_agent.cbmc import Counterexample
from bmc_agent.cex_validator import CExOutcome, ValidationResult
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient
from bmc_agent.parser import parse_c_file
from bmc_agent.realism_checker import RealismChecker
from bmc_agent.spec import Spec


def _is_blocked_realism(bug_report: dict) -> bool:
    """Did the original realism call fail in a way we should retry?"""
    rc = (bug_report.get("report") or {}).get("realism_check") or {}
    reasoning = (rc.get("reasoning") or "").lower()
    verdict = (rc.get("verdict") or "").lower()
    if verdict != "uncertain":
        return False
    blocked_markers = (
        "workspace api usage limits",
        "llm call failed",
        "could not parse llm response",
    )
    return any(m in reasoning for m in blocked_markers)


def _load_cex(report: dict) -> Counterexample:
    cex_data = report.get("counterexample") or {}
    return Counterexample(
        failing_property=cex_data.get("failing_property", "<unknown>"),
        variable_assignments=cex_data.get("variable_assignments") or {},
        trace=cex_data.get("trace") or [],
        description=cex_data.get("description", "") or "",
        failure_location=cex_data.get("failure_location") or {},
    )


def _load_spec(spec_json_path: Path, fn_name: str) -> Spec:
    if not spec_json_path.exists():
        return Spec(function_name=fn_name, precondition="", postcondition="")
    try:
        with open(spec_json_path) as f:
            data = json.load(f)
        return Spec.from_dict(data)
    except Exception:
        return Spec(function_name=fn_name, precondition="", postcondition="")


def _file_stem_to_source(corpus: Path, stem: str) -> Path | None:
    """Map a sweep sub-directory name back to a .c file in the corpus."""
    candidate = corpus / f"{stem}.c"
    if candidate.exists():
        return candidate
    return None


def _process_one(
    bug_report_path: Path,
    corpus: Path,
    config: Config,
    llm: LLMClient,
    log: logging.Logger,
) -> tuple[str, str]:
    """Returns (status, message). status in {SKIP, OK, ERROR}."""
    try:
        with open(bug_report_path) as f:
            doc = json.load(f)
    except Exception as e:
        return "ERROR", f"load failed: {e}"

    report = doc.get("report") or {}
    if not _is_blocked_realism(doc):
        return "SKIP", "realism already populated or not retry-eligible"

    fn_name = report.get("function_name") or ""
    if not fn_name:
        return "SKIP", "no function_name"

    fn_dir = bug_report_path.parent          # .../<file_stem>/<fn>/
    file_stem_dir = fn_dir.parent            # .../<file_stem>/
    file_stem = file_stem_dir.name           # "archive_read_support_format_cab"

    source_path = _file_stem_to_source(corpus, file_stem)
    if source_path is None:
        return "ERROR", f"source file for stem '{file_stem}' not found in corpus"

    parsed = parse_c_file(source_path)
    func = parsed.get_function_info(fn_name)
    if func is None:
        return "SKIP", f"function '{fn_name}' not parsed from {source_path.name}"

    cex = _load_cex(report)
    spec = _load_spec(fn_dir / "spec.json", fn_name)

    vr = ValidationResult(
        function_name=fn_name,
        counterexample=cex,
        caller_path=list(report.get("call_chain") or []),
        system_entry_input=report.get("reproducer"),
        refinement_history=[],
        final_precondition=spec.precondition,
        reasoning=report.get("reasoning_trail", "") or "",
        outcome=CExOutcome.REAL_BUG,
        system_entry_reached=(
            (report.get("confidence") or "") == "confirmed_system_entry"
        ),
    )

    # parsed.functions is dict[str, FunctionSignature]; RealismChecker wants
    # dict[str, FunctionInfo]. Build via get_function_info, dropping any None
    # the parser couldn't materialize.
    all_funcs = {}
    for name in (parsed.functions or {}):
        info = parsed.get_function_info(name)
        if info is not None:
            all_funcs[name] = info

    checker = RealismChecker(config, llm)
    try:
        result = checker.check(
            func=func,
            counterexample=cex,
            validation_result=vr,
            parsed_file=parsed,
            all_funcs=all_funcs,
            spec=spec,
        )
    except Exception:
        return "ERROR", f"realism crashed:\n{traceback.format_exc()[:600]}"

    # Update bug_report.json in place. Preserve original verdict in a side
    # field so we can audit conversion rates.
    prior = report.get("realism_check") or {}
    report["realism_check_original"] = prior
    report["realism_check"] = result.to_dict()
    report["realism_check"]["source"] = "rerun_openrouter_2026-05-24"

    tmp = bug_report_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    tmp.replace(bug_report_path)

    return "OK", f"{result.verdict.value} (was uncertain)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", required=True, help="sweep output dir")
    ap.add_argument("--corpus", required=True, help="source .c file dir")
    ap.add_argument("--log", default="/tmp/rerun_realism.log")
    args = ap.parse_args()

    logging.basicConfig(
        filename=args.log,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("rerun_realism")

    sweep = Path(args.sweep).resolve()
    corpus = Path(args.corpus).resolve()
    if not sweep.is_dir() or not corpus.is_dir():
        print(f"sweep or corpus dir missing: {sweep} / {corpus}", file=sys.stderr)
        return 2

    config = Config.from_env()
    config.enable_realism_check = True
    # realism_checker calls llm.complete() without a role= argument, so we
    # cannot rely on per-role routing — set the global LLM fields directly.
    llm = LLMClient(config)

    bug_reports = sorted(sweep.rglob("bug_report.json"))
    log.info("found %d bug_report.json files", len(bug_reports))
    counts = {"OK": 0, "SKIP": 0, "ERROR": 0}
    verdict_counts: dict[str, int] = {}
    for br in bug_reports:
        status, msg = _process_one(br, corpus, config, llm, log)
        counts[status] += 1
        log.info("%s %s :: %s", status, br.relative_to(sweep), msg)
        if status == "OK":
            v = msg.split(" ", 1)[0]
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
        # Flush each line so progress is visible in tail -f
        for h in log.handlers:
            try:
                h.flush()
            except Exception:
                pass

    log.info("DONE counts=%s verdicts=%s", counts, verdict_counts)
    print(f"DONE  counts={counts}  verdicts={verdict_counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
