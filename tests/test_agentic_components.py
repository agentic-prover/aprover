"""Under --agentic: classifier/realism/triage OFF by default + independently opt-in;
dynamic reproducer ON. Classifier-off surfaces the cex as a raw UNRESOLVED lead."""
import os
from bmc_agent.cli import build_parser, _apply_provider_args
from bmc_agent.config import Config


def _cfg(argv):
    for k in ("BMC_AGENT_ENABLE_CLASSIFIER", "BMC_AGENT_ENABLE_REALISM_CHECK",
              "BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER"):
        os.environ.pop(k, None)
    a = build_parser().parse_args(argv)
    c = Config.from_env()
    _apply_provider_args(c, a)
    return a, c


_BASE = ["verify-dir", "--source-dir", "x", "--driver", "d"]


def test_agentic_keeps_classifier_off_realism_triage_keeps_dynval():
    # The classifier drives the spurious->refinement->soundness-gate loop, so it
    # MUST stay on under --agentic. Only realism (exploitability downgrade) and
    # triage are off by default. Dynamic reproducer on.
    _, c = _cfg(_BASE + ["--agentic"])
    assert c.enable_classifier is True           # refinement + soundness gate stay live
    assert c.enable_realism_check is False
    assert c.enable_phase_3e_triage is False
    assert c.enable_dynamic_validation is True   # reproducer on


def test_per_component_optin_wins_under_agentic():
    # realism opts back in; classifier already on (refinement intact).
    _, c2 = _cfg(_BASE + ["--agentic", "--enable-realism-check"])
    assert c2.enable_realism_check is True
    assert c2.enable_classifier is True


def test_components_independent_no_agentic_defaults_on():
    _, c = _cfg(_BASE)   # no --agentic
    assert c.enable_classifier is True            # default on
    assert c.enable_dynamic_validation is True


def test_classifier_env_gate():
    os.environ["BMC_AGENT_ENABLE_CLASSIFIER"] = "false"
    try:
        assert Config.from_env().enable_classifier is False
    finally:
        os.environ.pop("BMC_AGENT_ENABLE_CLASSIFIER", None)


def test_classifier_off_short_circuits_validate_to_unresolved():
    from bmc_agent.cex_validator import CExValidator, CExOutcome
    from bmc_agent.cbmc import Counterexample
    v = CExValidator.__new__(CExValidator)
    class _Cfg: enable_classifier = False
    v.config = _Cfg()
    class _Sig: name = "f"; return_type = "int"; parameters = []
    class _Func: name = "f"; signature = _Sig(); body = ""; source_file = "x.c"
    class _Spec: precondition = "true"
    cex = Counterexample(failing_property="f.pointer.1", description="d")
    res = v.validate(_Func(), _Spec(), cex, {}, {}, None, "drv")
    assert res.outcome == CExOutcome.UNRESOLVED
    assert res.counterexample is cex


def test_agentic_enables_cbmc_driver_agents():
    # --agentic keeps the agentic CBMC driver on: flag selection (checks+unwind)
    # and the inlining advisor (which callee to inline vs stub), both code-reading.
    _, c = _cfg(_BASE + ["--agentic"])
    assert c.enable_flag_selection is True
    assert c.enable_inlining_advisor is True


def test_cbmc_driver_role_decoupled_and_agentic():
    # The CBMC-config agents (flag selector + inlining advisor) have their OWN
    # role `cbmc_driver`, so they can be agentic INDEPENDENTLY of spec_gen — e.g.
    # spec-gen fast on OpenRouter while the CBMC driver reads the code.
    import os
    for k in list(os.environ):
        if k.startswith("BMC_AGENT_LLM") or k == "BMC_AGENT_ENABLE_CLASSIFIER":
            os.environ.pop(k, None)
    os.environ["BMC_AGENT_LLM_SPEC_GEN_PROVIDER"] = "openai"   # keep spec-gen fast/flat
    try:
        from bmc_agent.cli import build_parser, _apply_provider_args
        from bmc_agent.config import Config
        from bmc_agent.llm import agentic_system_prompt
        a = build_parser().parse_args(_BASE + ["--agentic-claude-code"])
        c = Config.from_env(); _apply_provider_args(c, a)
        assert c.role_settings("spec_gen")["provider"] == "openai"        # fast
        assert c.role_settings("cbmc_driver")["provider"] == "claude-code"  # agentic, independent
        assert "[Agentic mode]" in agentic_system_prompt(c, "cbmc_driver", "x")
    finally:
        os.environ.pop("BMC_AGENT_LLM_SPEC_GEN_PROVIDER", None)
