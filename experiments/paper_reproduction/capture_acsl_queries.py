"""Capture archived assertion-synthesis solver inputs without model/solver calls.

Invoke in isolated processes with either the June-22 or candidate PYTHONPATH.
The solver is intercepted after writing its input; no verdict is validated here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import traceback

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--checkout", type=Path, required=True)
parser.add_argument("--recorded-root", type=Path, required=True, help="Directory containing case/attempt_*/synthesized_specs_source.json")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.checkout.resolve()
output = args.output.resolve()
sys.path.insert(0, str(root))
for key in list(os.environ):
    if key.startswith(("BMC_", "AMC_", "SVCOMP_", "K2")):
        os.environ.pop(key)
os.environ["BMC_AGENT_DSL_FILTER_POLICY"] = "2026-06-22"
os.environ["BMC_AGENT_CANONICALIZE_SPEC_BUFFER_EXTENTS"] = "false"
os.environ["BMC_AGENT_C_HARNESS_POLICY"] = "2026-06-22"

from bmc_agent.config import Config
from bmc_agent.cbmc import CBMCResult
from bmc_agent import assert_driven_specs as synth
from bmc_agent.source_parser import parse_source_file
from bmc_agent.harness_generator import _c_expressible_postcondition
from bmc_agent.llm import LLMClient
import bmc_agent.bmc_engine as engine_module

def forbidden_model(*args, **kwargs):
    raise AssertionError("model calls forbidden in this solver-input audit")

LLMClient.complete = forbidden_model
captured = []
def capture(harness_path, **kwargs):
    data = Path(harness_path).read_text()
    captured.append({"harness": data, "solver_parameters": kwargs})
    return CBMCResult(verified=True, raw_output="AUDIT_CAPTURE_ONLY")

engine_module.run_cbmc = capture
original = args.recorded_root.resolve()
recorded_inputs = sorted(original.glob("*/attempt_*/synthesized_specs_source.json"))
if not recorded_inputs:
    parser.error("no recorded assertion-synthesis inputs found")
output.mkdir(parents=True, exist_ok=False)
rows = []
for recorded in recorded_inputs:
    data = json.loads(recorded.read_text())
    source = recorded.parent / "source.c"
    text = source.read_text()
    label = f"{recorded.parent.parent.name}/{recorded.parent.name}"
    row = {"trial": label, "recorded_contract_sha256": hashlib.sha256(recorded.read_bytes()).hexdigest(),
           "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "queries": []}
    context = None
    try:
        if not synth.extract_goals(text):
            row["skipped"] = "contract mining path; no assertion-synthesis engine context"
            rows.append(row)
            continue
        parsed = parse_source_file(str(source), source_text=text)
        entry = synth._resolve_entry(parsed, data["entry"])
        callees = [c for c in synth.called_functions(text, list(parsed.functions)) if c != entry]
        contracts = data["synthesized_specs"]
        pre = {c: contracts.get(c, {}).get("requires", "true") for c in callees}
        post = {c: contracts.get(c, {}).get("ensures", "true") for c in callees}
        config = Config.from_env()
        config.artifact_dir = output / label
        config.llm_provider = "openai"
        config.llm_api_key = "offline-audit"
        config.llm_base_url = "http://127.0.0.1:1/v1"
        config.math_ints = True
        config.oracle = "frama-c"
        config.enable_split_spec_gen = True
        config.enable_agentic_harness_repair = False
        if hasattr(config, "canonicalize_spec_buffer_extents"):
            config.canonicalize_spec_buffer_extents = False
        context = synth._engine_context(source, text, config, entry, callees)
        if context is None:
            row["context_unavailable"] = True
        else:
            eligible = {c: _c_expressible_postcondition(post[c], [n for _, n in parsed.get_function_info(c).signature.parameters]) is not None for c in callees}
            calls = [("sufficiency", all(eligible.values()), lambda: synth._suff_engine(context, callees, pre, post))]
            for callee in callees:
                calls.append(("soundness_" + callee, eligible[callee], lambda c=callee: synth._sound_engine(context, c, pre[c], post[c])))
            for name, selected, call in calls:
                captured.clear()
                verdict = call()
                query = {"name": name, "engine_eligible_for_final_contract": selected,
                         "error": getattr(verdict, "error", None), "invocations": []}
                for i, item in enumerate(captured):
                    harness = item.pop("harness")
                    tu_path = str(context["parsed"].path)
                    normalized = harness.replace(tu_path, "<TRANSLATED_TU>").replace(str(output), "<OUTPUT>")
                    dest = output / label / f"{name}_{i}.normalized.c"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(normalized)
                    item.update(harness_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
                                normalized_harness=str(dest.relative_to(output)))
                    query["invocations"].append(item)
                row["queries"].append(query)
    except Exception:
        row["error"] = traceback.format_exc()
    finally:
        if context is not None:
            path = Path(context["parsed"].path)
            if path.parent == Path("/tmp"):
                path.unlink(missing_ok=True)
    rows.append(row)
(output / "queries.json").write_text(json.dumps({"source_root": str(root), "model_calls": 0,
    "solver_calls": 0, "solver_intercepted": True,
    "source_commit": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
    "source_hashes": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root / "bmc_agent").rglob("*.py"))},
    "capture_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "normalization": "translated temporary TU and output root only",
    "rows": rows}, indent=2) + "\n")
print(json.dumps({"records": len(rows), "errors": sum("error" in r for r in rows),
                  "contexts_unavailable": sum(r.get("context_unavailable", False) for r in rows),
                  "queries": sum(len(r["queries"]) for r in rows)}))
raise SystemExit(1 if any("error" in r for r in rows) else 0)
