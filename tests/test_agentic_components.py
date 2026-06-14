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


def test_agentic_keeps_classifier_on_lightweight_realism_on_triage_off_dynval_on():
    # The classifier drives the spurious->refinement->soundness-gate loop, so it
    # MUST stay on under --agentic. A LIGHTWEIGHT (single-call, non-tool) realism
    # check is also on by default; the expensive realism TOOLS are opt-in. Triage
    # stays off. Dynamic reproducer on.
    _, c = _cfg(_BASE + ["--agentic"])
    assert c.enable_classifier is True           # refinement + soundness gate stay live
    assert c.enable_realism_check is True         # lightweight realism on by default
    assert c.enable_realism_tools is False        # expensive tool-use realism is opt-in
    assert c.enable_phase_3e_triage is False
    assert c.enable_dynamic_validation is True   # reproducer on


def test_agentic_is_default_on():
    # --agentic is now the DEFAULT: a bare invocation already runs the agentic
    # stack (soundness gate + agentic harness-repair + split spec-gen), with no
    # backend forced.
    a, c = _cfg(_BASE)
    assert a.agentic is True
    assert c.enable_soundness_gate is True
    assert c.enable_agentic_harness_repair is True
    assert c.enable_split_spec_gen is True


def test_no_agentic_disables_the_stack():
    # --no-agentic is the escape hatch to the plain non-agentic core: the agentic
    # gating block does not run, so the agentic-only layers (soundness gate,
    # split spec-gen) are off. The conventional core (CBMC config, dynamic
    # validation) keeps its own Config defaults. NOTE: agentic harness-repair is
    # now a default-ON fail-safe fallback (decoupled from --agentic), so it stays
    # on here; --no-agentic-harness-repair is its dedicated off-switch.
    a, c = _cfg(_BASE + ["--no-agentic"])
    assert a.agentic is False
    assert c.enable_soundness_gate is False
    assert c.enable_agentic_harness_repair is True
    assert c.enable_split_spec_gen is False


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
