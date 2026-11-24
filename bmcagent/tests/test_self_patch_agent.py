"""Tests for the self-patch agent (Phase 3 of autonomous mode).

All LLM calls are mocked — these tests run with zero API cost. The
agent's behaviour is fully driven by the safety gates and the
structured JSON contract with the LLM, so unit-level coverage is
high-leverage.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bmc_agent.cbmc_error_classifier import (
    CbmcErrorClass,
    CbmcErrorDiagnosis,
)
from bmc_agent.self_patch_agent import (
    PatchMode,
    PatchProposal,
    ProposalStatus,
    SelfPatchAgent,
    _count_diff_lines,
    _parse_diff_targets,
    _parse_json_response,
    _resolve_mode,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _Config:
    """Minimal stub mimicking the config fields the agent reads."""
    allow_self_patch: str = "stage"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""


class _StubLLM:
    """Stub LLMClient that returns a pre-baked response. No network."""

    def __init__(self, response: str | None = None, raise_exc: Exception | None = None):
        self.response = response or ""
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def complete(self, system_prompt, user_prompt, max_tokens=4096, temperature=0.0):
        self.calls.append({
            "system": system_prompt,
            "user": user_prompt,
            "max_tokens": max_tokens,
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _diag(cls: CbmcErrorClass = CbmcErrorClass.UNKNOWN, **kw) -> CbmcErrorDiagnosis:
    return CbmcErrorDiagnosis(error_class=cls, **kw)


def _valid_patch_response() -> str:
    """A well-formed JSON proposal touching an allowed file with a
    regression test in tests/. Used for happy-path gate tests.
    """
    diff = (
        "--- a/bmc_agent/harness_generator.py\n"
        "+++ b/bmc_agent/harness_generator.py\n"
        "@@ -1,3 +1,4 @@\n"
        " line a\n"
        "+# self-patch test marker\n"
        " line b\n"
        " line c\n"
    )
    test_src = (
        "def test_self_patch_smoke():\n"
        "    assert True\n"
    )
    import json
    return json.dumps({
        "action": "patch",
        "rationale": "Synthetic patch for unit testing.",
        "diff": diff,
        "regression_test_path": "tests/test_self_patch_smoke.py",
        "regression_test_source": test_src,
        "regression_test_name": "test_self_patch_smoke",
    })


# ---------------------------------------------------------------------------
# _resolve_mode
# ---------------------------------------------------------------------------


def test_resolve_mode_defaults_to_deny():
    class _Bare: pass
    assert _resolve_mode(_Bare()) == PatchMode.DENY


def test_resolve_mode_accepts_enum_value():
    cfg = _Config(allow_self_patch="stage")
    assert _resolve_mode(cfg) == PatchMode.STAGE


def test_resolve_mode_unknown_falls_back_to_deny():
    cfg = _Config(allow_self_patch="banana")
    assert _resolve_mode(cfg) == PatchMode.DENY


def test_resolve_mode_accepts_PatchMode_directly():
    cfg = _Config(allow_self_patch=PatchMode.AUTO)
    assert _resolve_mode(cfg) == PatchMode.AUTO


# ---------------------------------------------------------------------------
# Deny short-circuit
# ---------------------------------------------------------------------------


def test_propose_short_circuits_when_mode_deny(tmp_path):
    cfg = _Config(allow_self_patch="deny")
    llm = _StubLLM(response="should not be called")
    agent = SelfPatchAgent(llm=llm, repo_root=tmp_path, config=cfg)
    prop = agent.propose(_diag(), "fn", "/dev/null", "")
    assert prop.status == ProposalStatus.DENIED
    assert llm.calls == []
    assert "deny" in prop.rejection_reason


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def test_parse_json_response_bare():
    import json
    out = _parse_json_response('{"action": "patch", "rationale": "x"}')
    assert out["action"] == "patch"


def test_parse_json_response_fenced():
    text = '```json\n{"action": "give_up", "rationale": "no fix"}\n```'
    out = _parse_json_response(text)
    assert out["action"] == "give_up"


def test_parse_json_response_invalid_raises():
    with pytest.raises(ValueError):
        _parse_json_response("definitely not json at all")


# ---------------------------------------------------------------------------
# Propose path
# ---------------------------------------------------------------------------


def test_propose_returns_PROPOSED_on_valid_response(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    llm = _StubLLM(response=_valid_patch_response())
    agent = SelfPatchAgent(llm=llm, repo_root=tmp_path, config=cfg)
    prop = agent.propose(_diag(), "fn", "/dev/null", "")
    assert prop.status == ProposalStatus.PROPOSED
    assert "harness_generator.py" in prop.diff
    assert prop.regression_test_path.startswith("tests/")
    assert prop.regression_test_name == "test_self_patch_smoke"


def test_propose_returns_REJECTED_on_give_up(tmp_path):
    import json
    cfg = _Config(allow_self_patch="stage")
    llm = _StubLLM(response=json.dumps({
        "action": "give_up",
        "rationale": "Can't fix without bigger refactor.",
    }))
    agent = SelfPatchAgent(llm=llm, repo_root=tmp_path, config=cfg)
    prop = agent.propose(_diag(), "fn", "/dev/null", "")
    assert prop.status == ProposalStatus.REJECTED
    assert "gave up" in prop.rejection_reason


def test_propose_returns_LLM_ERROR_on_non_json(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    llm = _StubLLM(response="here you go: <not json>")
    agent = SelfPatchAgent(llm=llm, repo_root=tmp_path, config=cfg)
    prop = agent.propose(_diag(), "fn", "/dev/null", "")
    assert prop.status == ProposalStatus.LLM_ERROR


def test_propose_returns_LLM_ERROR_on_llm_exception(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    llm = _StubLLM(raise_exc=RuntimeError("rate-limited"))
    agent = SelfPatchAgent(llm=llm, repo_root=tmp_path, config=cfg)
    prop = agent.propose(_diag(), "fn", "/dev/null", "")
    assert prop.status == ProposalStatus.LLM_ERROR
    assert "rate-limited" in prop.rejection_reason


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


def test_parse_diff_targets_extracts_allowed_paths():
    diff = (
        "--- a/bmc_agent/harness_generator.py\n"
        "+++ b/bmc_agent/harness_generator.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    assert _parse_diff_targets(diff) == ["bmc_agent/harness_generator.py"]


def test_parse_diff_targets_skips_dev_null():
    diff = (
        "--- /dev/null\n"
        "+++ b/tests/new_test.py\n"
        "@@ -0,0 +1 @@\n"
        "+x\n"
    )
    assert _parse_diff_targets(diff) == ["tests/new_test.py"]


def test_parse_diff_targets_raises_on_empty():
    with pytest.raises(ValueError):
        _parse_diff_targets("")


def test_count_diff_lines_counts_added_and_removed():
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n"
        "-a\n"
        "+a2\n"
        " b\n"
    )
    # The +++ and --- file-header lines are excluded (they're two
    # consecutive +/- followed by a +/-, which the regex skips).
    assert _count_diff_lines(diff) == 2


# ---------------------------------------------------------------------------
# Validate (safety gates)
# ---------------------------------------------------------------------------


def _proposed(diff: str, test_path: str = "tests/test_x.py", test_src: str = "def test_x():\n    pass\n", test_name: str = "test_x") -> PatchProposal:
    return PatchProposal(
        status=ProposalStatus.PROPOSED,
        diff=diff,
        regression_test_path=test_path,
        regression_test_source=test_src,
        regression_test_name=test_name,
    )


def test_validate_rejects_disallowed_file(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    agent = SelfPatchAgent(llm=_StubLLM(), repo_root=tmp_path, config=cfg)
    diff = (
        "--- a/bmc_agent/bmc_engine.py\n"
        "+++ b/bmc_agent/bmc_engine.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    prop = agent.validate(_proposed(diff))
    assert prop.status == ProposalStatus.REJECTED
    assert "non-allowed" in prop.rejection_reason


def test_validate_rejects_too_many_files(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    agent = SelfPatchAgent(llm=_StubLLM(), repo_root=tmp_path, config=cfg)
    diff = (
        "--- a/bmc_agent/harness_generator.py\n"
        "+++ b/bmc_agent/harness_generator.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "--- a/bmc_agent/preprocessor.py\n"
        "+++ b/bmc_agent/preprocessor.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "--- a/bmc_agent/cbmc.py\n"
        "+++ b/bmc_agent/cbmc.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    prop = agent.validate(_proposed(diff))
    assert prop.status == ProposalStatus.REJECTED
    # Either 'non-allowed' (cbmc.py isn't in the allow-list) or
    # 'files >' (cap exceeded) is an acceptable rejection — both
    # are correct safety outcomes.
    assert prop.rejection_reason


def test_validate_rejects_regression_test_outside_tests_dir(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    agent = SelfPatchAgent(llm=_StubLLM(), repo_root=tmp_path, config=cfg)
    diff = (
        "--- a/bmc_agent/harness_generator.py\n"
        "+++ b/bmc_agent/harness_generator.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    prop = agent.validate(_proposed(diff, test_path="not_tests/x.py"))
    assert prop.status == ProposalStatus.REJECTED
    assert "tests/" in prop.rejection_reason


def test_validate_rejects_empty_regression_test_source(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    agent = SelfPatchAgent(llm=_StubLLM(), repo_root=tmp_path, config=cfg)
    diff = (
        "--- a/bmc_agent/harness_generator.py\n"
        "+++ b/bmc_agent/harness_generator.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    prop = agent.validate(_proposed(diff, test_src=""))
    assert prop.status == ProposalStatus.REJECTED
    assert "regression_test_source" in prop.rejection_reason


def test_validate_passes_minimal_valid_proposal(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    agent = SelfPatchAgent(llm=_StubLLM(), repo_root=tmp_path, config=cfg)
    diff = (
        "--- a/bmc_agent/harness_generator.py\n"
        "+++ b/bmc_agent/harness_generator.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    prop = agent.validate(_proposed(diff))
    # Structural gates pass — status unchanged from PROPOSED. The
    # fail-before / pass-after gates run in stage_or_apply, not here.
    assert prop.status == ProposalStatus.PROPOSED
    assert prop.files_touched == ["bmc_agent/harness_generator.py"]


def test_validate_short_circuits_on_non_PROPOSED_status(tmp_path):
    cfg = _Config(allow_self_patch="stage")
    agent = SelfPatchAgent(llm=_StubLLM(), repo_root=tmp_path, config=cfg)
    p = PatchProposal(status=ProposalStatus.DENIED, rejection_reason="x")
    out = agent.validate(p)
    # Pass-through unchanged.
    assert out.status == ProposalStatus.DENIED


# ---------------------------------------------------------------------------
# stage_or_apply — deny short-circuits without touching disk
# ---------------------------------------------------------------------------


def test_stage_or_apply_deny_short_circuits(tmp_path):
    cfg = _Config(allow_self_patch="deny")
    agent = SelfPatchAgent(llm=_StubLLM(), repo_root=tmp_path, config=cfg)
    p = _proposed(
        "--- a/bmc_agent/harness_generator.py\n"
        "+++ b/bmc_agent/harness_generator.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    p = agent.validate(p)
    out = agent.stage_or_apply(p, tmp_path, round_idx=0)
    assert out.status == ProposalStatus.DENIED
    # No artifacts written.
    assert not (tmp_path / "proposed_patches").exists()
