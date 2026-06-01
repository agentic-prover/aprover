from __future__ import annotations

import json
import zipfile
from pathlib import Path

from experiments.spec_quality_compare.autospec_full_repro import (
    _adapt_gptcore,
    bmc_agent_openrouter_env,
    build_manifest_from_zip,
    parse_final_result,
    parse_wp_output,
    reconcile_raw,
    resolve_openrouter_model,
    scan_for_secret_text,
    summarize_statuses,
)


def test_manifest_filters_sources_verified_mutants_and_pilot(tmp_path: Path) -> None:
    archive = tmp_path / "AutoSpec.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("AutoSpec/benchmark/fib_46_benchmark/01.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/fib_46_benchmark/01_marked.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/code2inv_133_benchmark/1.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/SVCOMP/quantifier/array_true-unreach-call1/array_true-unreach-call1.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/frama-c-problems/general_wp_problems/max_of_2.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/frama-c-problems/general_wp_problems/max_of_2_verified.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/frama-c-problems/loops/3_verified2.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/X509-parser/parse_null.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/100mutants/03.c", "void main(){}")
        zf.writestr("AutoSpec/benchmark/100mutants/03_mutation_1.c", "void main(){}")

    manifest = build_manifest_from_zip(archive, autospec_root=tmp_path / "AutoSpec")

    assert len(manifest["case_sets"]["official_251_candidates"]) == 4
    assert len(manifest["case_sets"]["verified_annotations"]) == 2
    assert len(manifest["case_sets"]["mutants_100"]) == 1
    assert len(manifest["case_sets"]["mutant_seeds"]) == 1
    assert len(manifest["case_sets"]["x509_extra"]) == 1
    assert manifest["case_sets"]["pilot10"]
    assert manifest["warnings"]


def test_parse_final_result_handles_pass_fail_and_metadata() -> None:
    parsed = parse_final_result(
        "Pass\n{'Iteration': 2, 'Status': 1, 'tokens_usage': 1234}\n"
    )
    assert parsed["result"] == "pass"
    assert parsed["iteration"] == 2
    assert parsed["status"] == 1
    assert parsed["tokens_usage"] == 1234

    assert parse_final_result("Fail\n{}")["result"] == "fail"
    assert parse_final_result("")["result"] == "unknown"


def test_reconcile_raw_groups_final_results(tmp_path: Path) -> None:
    archive = tmp_path / "AutoSpec.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("AutoSpec/raw/out_framac/foo_0001/final_result", "Pass\n{}")
        zf.writestr("AutoSpec/raw/out_framac/bar_0001/final_result", "Fail\n{}")
        zf.writestr("AutoSpec/raw/out_FIB/01_0001/final_result", "Pass\n{}")

    report = reconcile_raw(archive)

    assert report["total_final_results"] == 3
    assert report["by_raw_folder"]["out_framac"]["pass"] == 1
    assert report["by_raw_folder"]["out_framac"]["fail"] == 1
    assert report["by_raw_folder"]["out_FIB"]["pass"] == 1


def test_parse_wp_output_classifies_proved_unproved_annotation_timeout_and_error() -> None:
    proved = parse_wp_output("[wp] Proved goals:    6 / 6", "", 0)
    unproved = parse_wp_output("[wp] Proved goals:    5 / 6", "", 0)
    annot = parse_wp_output("", "wrong order of clause\n[kernel:annot-error]", 1)
    timeout = parse_wp_output("", "", None, timed_out=True)
    error = parse_wp_output("", "boom", 2)

    assert proved["status"] == "proved"
    assert unproved["status"] == "unproved"
    assert annot["status"] == "annotation_error"
    assert timeout["status"] == "timeout"
    assert error["status"] == "tool_error"


def test_secret_scan_flags_key_like_values_but_allows_redaction() -> None:
    assert scan_for_secret_text("OPENAI_API_KEY = <redacted>") == []
    fake_key = "sk-" + ("a" * 20)
    assert scan_for_secret_text("OPENAI_API_KEY = " + fake_key)
    assert scan_for_secret_text(fake_key)


def test_bmc_agent_env_maps_secret_file_to_openrouter_without_recording_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BMC_AGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_KEY", raising=False)
    secret = tmp_path / "openrouter.env"
    secret.write_text("OPENROUTER_API_KEY=test-openrouter-key\n", encoding="utf-8")

    env = bmc_agent_openrouter_env(secret_env=secret, model="claude-test")

    assert env["BMC_AGENT_LLM_API_KEY"] == "test-openrouter-key"
    assert env["BMC_AGENT_LLM_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["BMC_AGENT_LLM_PROVIDER"] == "openai"
    assert env["BMC_AGENT_LLM_MODEL"] == "claude-test"


def test_openrouter_claude_alias_resolves_to_valid_provider_model_id() -> None:
    assert resolve_openrouter_model("claude-sonnet-4-6") == "anthropic/claude-sonnet-4.6"
    assert resolve_openrouter_model("gpt-3.5-turbo") == "gpt-3.5-turbo"


def test_adapt_gptcore_falls_back_for_non_openai_model_ids(tmp_path: Path) -> None:
    path = tmp_path / "gptcore.py"
    path.write_text(
        "def num_tokens_from_messages(messages, model='x'):\n"
        "    else:\n"
        "        raise NotImplementedError(\n"
        "            f\"\"\"num_tokens_from_messages() is not implemented for model {model}. See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens.\"\"\"\n"
        "        )\n",
        encoding="utf-8",
    )

    _adapt_gptcore(path)

    text = path.read_text(encoding="utf-8")
    assert "raise NotImplementedError" not in text
    assert 'model="gpt-3.5-turbo-0613"' in text


def test_summary_keeps_methods_separate() -> None:
    rows = [
        {"method": "autospec_gpt35_openrouter", "status": "pass"},
        {"method": "autospec_claude_openrouter", "status": "fail"},
        {"method": "bmc_agent_native_acsl", "status": "success"},
    ]
    summary = summarize_statuses(rows)

    assert summary["statuses"] == {"fail": 1, "pass": 1, "success": 1}
    assert summary["methods"]["autospec_gpt35_openrouter"] == 1
    assert summary["methods"]["autospec_claude_openrouter"] == 1
    assert summary["methods"]["bmc_agent_native_acsl"] == 1
