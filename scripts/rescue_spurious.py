"""
Run the realism check on every classification.json with outcome=spurious.
The pipeline currently DROPS these findings before they reach the realism
stage. This script asks the realism LLM whether the classifier was right.

A REALISTIC verdict here indicates a missed real bug — the classifier
heuristic (path-divergent-unwind filter OR "no caller can produce
state") false-rejected a finding that the LLM, with full source +
witness + call-chain context, identifies as plausible.

Output: <fn_dir>/rescue_realism.json with the realism verdict + reasoning.
Original classification.json and bug_report.json are NEVER modified.

Usage:
    .venv/bin/python scripts/rescue_spurious.py \
        --sweep /tmp/libarchive_n3_full_out/seedhunt_n3 \
        --corpus /tmp/libarchive_seedhunt_full \
        --log /tmp/rescue_spurious.log
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bmc_agent.cbmc import Counterexample
from bmc_agent.cex_validator import CExOutcome, ValidationResult
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient
from bmc_agent.parser import parse_c_file
from bmc_agent import realism_checker as _rc_mod
from bmc_agent.realism_checker import RealismChecker
from bmc_agent.spec import Spec


# The rescue script's whole point is to bypass the pre-LLM heuristic
# detectors that already rejected these findings — otherwise we just
# get the same "unrealistic" verdict from the same logic. Stub each
# detector to return None so realism.check() always reaches the LLM.
_WITNESS_DETECTORS = (
    "_witness_indicates_uninitialized_library",
    "_witness_indicates_jv_stub_disconnect",
    "_witness_indicates_null_guard_violation",
    "_witness_indicates_usb_serial_framework_invariant",
    "_witness_indicates_phy_framework_invariant",
    "_witness_indicates_netdev_private_framework_invariant",
    "_witness_indicates_intentional_truncation",
    "_witness_indicates_path_divergent_unwind",
)
for _det_name in _WITNESS_DETECTORS:
    if hasattr(_rc_mod, _det_name):
        setattr(_rc_mod, _det_name, lambda *a, **kw: None)


def _load_cex(cls: dict) -> Counterexample:
    cex_data = cls.get("counterexample") or {}
    return Counterexample(
        failing_property=cex_data.get("failing_property", "<unknown>"),
        variable_assignments=cex_data.get("variable_assignments") or {},
        trace=cex_data.get("trace") or [],
        description=cex_data.get("description", "") or "",
        failure_location=cex_data.get("failure_location") or {},
    )


def _process_one(
    classification_path: Path,
    corpus: Path,
    config: Config,
    llm: LLMClient,
    log: logging.Logger,
) -> tuple[str, str]:
    """Returns (status, message)."""
    try:
        with open(classification_path) as f:
            doc = json.load(f)
    except Exception as e:
        return "ERROR", f"load: {e}"

    cls = doc.get("classification") or {}
    if cls.get("outcome") != "spurious":
        return "SKIP", f"outcome={cls.get('outcome')}"

    fn_dir = classification_path.parent
    rescue_path = fn_dir / "rescue_realism.json"
    if rescue_path.exists():
        return "SKIP", "rescue_realism.json already exists"

    fn_name = cls.get("function_name") or ""
    file_stem = fn_dir.parent.name
    source_path = corpus / f"{file_stem}.c"
    if not source_path.exists():
        return "ERROR", f"source missing: {source_path}"

    parsed = parse_c_file(source_path)
    func = parsed.get_function_info(fn_name)
    if func is None:
        return "SKIP", f"function '{fn_name}' not parsed"

    cex = _load_cex(cls)

    # Load spec next to classification.json if present
    spec_path = fn_dir / "spec.json"
    if spec_path.exists():
        try:
            spec = Spec.from_dict(json.load(open(spec_path)))
        except Exception:
            spec = Spec(function_name=fn_name, precondition="", postcondition="")
    else:
        spec = Spec(function_name=fn_name, precondition="", postcondition="")

    # Force REAL_BUG so the realism checker actually runs (it skips on
    # other outcomes). The rescue interpretation: we're asking the LLM
    # "if you treat this as a candidate real bug, is it plausible?"
    vr = ValidationResult(
        function_name=fn_name,
        counterexample=cex,
        caller_path=list(cls.get("caller_path") or []),
        system_entry_input=None,
        refinement_history=list(cls.get("refinement_history") or []),
        final_precondition=spec.precondition,
        reasoning=cls.get("reasoning", "") or "",
        outcome=CExOutcome.REAL_BUG,
        system_entry_reached=bool(cls.get("system_entry_reached", False)),
    )

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
        return "ERROR", f"realism crashed:\n{traceback.format_exc()[:500]}"

    out = {
        "source": "rescue_spurious_2026-05-24",
        "function_name": fn_name,
        "failing_property": cex.failing_property,
        "classifier_reason": (cls.get("reasoning") or "")[:300],
        "rescue_verdict": result.verdict.value,
        "rescue_reasoning": result.reasoning,
        "rescue_key_concern": result.key_concern,
        "rescue_llm_confidence": result.llm_confidence,
    }
    with open(rescue_path, "w") as f:
        json.dump(out, f, indent=2)

    return "OK", result.verdict.value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--log", default="/tmp/rescue_spurious.log")
    args = ap.parse_args()

    logging.basicConfig(
        filename=args.log,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("rescue_spurious")

    sweep = Path(args.sweep).resolve()
    corpus = Path(args.corpus).resolve()
    if not sweep.is_dir() or not corpus.is_dir():
        print(f"missing dir: {sweep} / {corpus}", file=sys.stderr)
        return 2

    config = Config.from_env()
    config.enable_realism_check = True
    llm = LLMClient(config)

    cls_files = sorted(sweep.rglob("classification.json"))
    log.info("found %d classification.json files", len(cls_files))
    counts = {"OK": 0, "SKIP": 0, "ERROR": 0}
    verdict_counts: dict[str, int] = {}
    for p in cls_files:
        status, msg = _process_one(p, corpus, config, llm, log)
        counts[status] += 1
        log.info("%s %s :: %s", status, p.relative_to(sweep), msg)
        if status == "OK":
            verdict_counts[msg] = verdict_counts.get(msg, 0) + 1
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
