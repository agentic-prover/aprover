"""General trust-boundary (threat-model) note support: injected into the
trust-deciding roles' system prompts, conservative-default attached, no-op
elsewhere, and centrally applied in LLM.complete / complete_with_tools."""
from bmc_agent.config import Config
from bmc_agent.llm import (
    threat_model_context,
    agentic_system_prompt,
    THREAT_MODEL_NOTE_ROLES,
)


def _cfg(note=""):
    c = Config()
    c.threat_model_note = note
    return c


def test_roles_allowlist_is_the_trust_deciding_set():
    assert THREAT_MODEL_NOTE_ROLES == frozenset({
        "spec_gen", "refinement", "classifier",
        "dynamic_repro", "dynval_triage", "realism",
    })


def test_note_injected_for_trust_deciding_role_with_conservative_rule():
    c = _cfg("buf/len are attacker-controlled; ctx is init'd by the caller.")
    block = threat_model_context(c, "spec_gen")
    assert "Trust boundary for this target" in block
    assert "attacker-controlled" in block
    assert "ctx is init'd" in block
    # the standing conservative-default instruction always rides along
    assert "ATTACKER-CONTROLLED unless" in block
    assert "masks the very bugs" in block


def test_note_not_injected_for_non_trust_role():
    c = _cfg("some note")
    assert threat_model_context(c, "cbmc_driver") == ""
    assert threat_model_context(c, "feedback_distill") == ""


def test_empty_note_is_noop_even_for_trust_role():
    assert threat_model_context(_cfg(""), "spec_gen") == ""
    assert threat_model_context(_cfg(""), "realism") == ""


def test_agentic_system_prompt_does_not_double_inject_note():
    # The note lives in complete()/complete_with_tools, NOT in
    # agentic_system_prompt — so a wrapped prompt must NOT contain it (else
    # wrapped call sites would get it twice).
    c = _cfg("attacker note here")
    c.claude_code_agentic = False
    out = agentic_system_prompt(c, "spec_gen", "SYS")
    assert "Trust boundary" not in out


def test_complete_injects_note_centrally(monkeypatch):
    """LLM.complete must append the note to the system prompt for a trust role,
    regardless of whether the call site wrapped its prompt."""
    from bmc_agent import llm as llm_mod

    c = _cfg("LEN is attacker-controlled.")
    client = llm_mod.LLMClient(c)

    captured = {}

    def fake_anthropic(system_prompt, user_prompt, max_tokens, temperature, api_kwargs):
        captured["sys"] = system_prompt
        return "ok"

    monkeypatch.setattr(client, "_complete_anthropic", fake_anthropic)
    # force the anthropic path
    monkeypatch.setattr(c, "resolved_provider", lambda: "anthropic")

    client.complete("BASE PROMPT", "u", role="spec_gen")
    assert "BASE PROMPT" in captured["sys"]
    assert "LEN is attacker-controlled." in captured["sys"]
    assert "Trust boundary for this target" in captured["sys"]


def test_complete_no_note_for_non_trust_role(monkeypatch):
    from bmc_agent import llm as llm_mod

    c = _cfg("LEN is attacker-controlled.")
    client = llm_mod.LLMClient(c)
    captured = {}

    def fake_anthropic(system_prompt, user_prompt, max_tokens, temperature, api_kwargs):
        captured["sys"] = system_prompt
        return "ok"

    monkeypatch.setattr(client, "_complete_anthropic", fake_anthropic)
    monkeypatch.setattr(c, "resolved_provider", lambda: "anthropic")

    client.complete("BASE", "u", role="cbmc_driver")
    assert captured["sys"] == "BASE"  # untouched
