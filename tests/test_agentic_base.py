"""Every BaseAgent becomes an *investigating* agent under --agentic."""
from bmc_agent.agents.refinement import RefinementAgent
from bmc_agent.config import Config
from bmc_agent.llm import LLMClient


def _agent(cfg):
    return RefinementAgent(cfg, LLMClient.__new__(LLMClient))


def test_no_framing_when_agentic_off():
    c = Config(); c.claude_code_agentic = False
    assert "[Agentic mode]" not in _agent(c)._system_prompt_for_call()


def test_framing_when_agentic_and_claude_code():
    c = Config(); c.claude_code_agentic = True
    c.llm_role_overrides = {"refinement": {"provider": "claude-code"}}
    c.claude_code_add_dirs = ["/proj/src"]
    sp = _agent(c)._system_prompt_for_call()
    assert "[Agentic mode]" in sp
    assert "/proj/src" in sp            # the source dir is cited so it can Read it
    assert "Read" in sp and "Grep" in sp


def test_no_framing_when_role_routed_elsewhere():
    # agentic on, but this agent's role is explicitly on a non-claude-code LLM
    c = Config(); c.claude_code_agentic = True; c.llm_provider = "openai"
    c.llm_role_overrides = {"refinement": {"provider": "openai"}}
    assert "[Agentic mode]" not in _agent(c)._system_prompt_for_call()


def test_all_agent_roles_includes_dynamic_repro_and_dynval(monkeypatch):
    """--agentic routes every agent role, incl. the dynamic-validation ones."""
    for k in ("BMC_AGENT_LLM_PROVIDER", "BMC_AGENT_LLM_DEFAULT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    from bmc_agent.cli import build_parser, _apply_provider_args
    a = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d", "--agentic"])
    cfg = Config.from_env()
    _apply_provider_args(cfg, a)
    for role in ("dynamic_repro", "dynval_triage"):
        assert cfg.role_settings(role)["provider"] == "claude-code", role
