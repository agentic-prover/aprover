"""The opt-in policy must change solver code, not just the configuration log."""
from bmc_agent.config import Config
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.parser import parse_c_file
from bmc_agent.spec_generator import _fallback_spec


def make_harness(tmp_path, monkeypatch, *, respect):
    monkeypatch.setenv("BMC_FAITHFUL_MAIN", "0")
    # Exercise the legacy truthy-string pitfall as well as forced entry inlining.
    monkeypatch.setenv("BMC_TRANSITIVE_INLINE", "0")
    source = tmp_path / "input.c"
    source.write_text("static int tiny(int x) { return x + 1; }\n"
                      "static int nested(int x) { return tiny(x); }\n"
                      "int main(void) { return tiny(1) + nested(2); }\n")
    parsed = parse_c_file(source)
    config = Config(artifact_dir=str(tmp_path / "artifacts"),
                    enable_bmc_config_agent=True,
                    respect_agent_inline_decisions=respect)
    config.agent_inline_overrides = {"main": {"nested"}}
    config.agent_inline_decisions = {"main": {"tiny": False, "nested": True}}
    return HarnessGenerator(config).generate_harness(
        parsed.get_function_info("main"), _fallback_spec("main"), parsed,
        all_funcs=parsed.functions)


def test_explicit_stub_survives_mechanical_and_transitive_inlining(tmp_path, monkeypatch):
    harness = make_harness(tmp_path, monkeypatch, respect=True)
    assert "Inlined real callee body: nested" in harness
    assert "Inlined real callee body: tiny" not in harness
    assert "tiny_stub(x)" in harness
    assert "tiny_stub(1)" in harness


def test_default_policy_keeps_existing_transitive_inlining(tmp_path, monkeypatch):
    harness = make_harness(tmp_path, monkeypatch, respect=False)
    assert "Inlined real callee body: nested" in harness
    assert "Inlined real callee body: tiny" in harness


def test_policy_is_opt_in_from_environment(monkeypatch):
    monkeypatch.delenv("BMC_AGENT_RESPECT_INLINE_DECISIONS", raising=False)
    assert Config.from_env().respect_agent_inline_decisions is False
    monkeypatch.setenv("BMC_AGENT_RESPECT_INLINE_DECISIONS", "true")
    assert Config.from_env().respect_agent_inline_decisions is True


def test_ldv_guard_keeps_check_state_writer_and_callers_but_not_unrelated_code(tmp_path, monkeypatch):
    monkeypatch.setenv("BMC_FAITHFUL_MAIN", "0")
    monkeypatch.setenv("SVCOMP_PROP", "unreach")
    source = tmp_path / "ldv.c"
    source.write_text('int ldv_mutex = 1;\n'
                      'void reach_error(void) { __CPROVER_assert(0, "target"); }\n'
                      'void write_state(void) { ldv_mutex = 2; }\n'
                      'void driver(void) { write_state(); }\n'
                      'int predicate(void) { return ldv_mutex == 1; }\n'
                      'void ldv_check_final_state(void) { if (!predicate()) reach_error(); }\n'
                      'int ldv__builtin_expect(int x) { return x; }\n'
                      'void expensive(void) { /* ldv_mutex = 2; */ for (int i=0;i<10000;i++) { ldv__builtin_expect(i); } }\n'
                      'int main(void) { expensive(); driver(); ldv_check_final_state(); return 0; }\n')
    parsed = parse_c_file(source)
    config = Config(enable_bmc_config_agent=True, respect_agent_inline_decisions=True,
                    enable_inlining_advisor=False)
    config.agent_inline_decisions = {"main": {name: False for name in parsed.functions}}
    generator = HarnessGenerator(config)
    before = generator.generate_harness(parsed.get_function_info("main"), _fallback_spec("main"), parsed)
    assert "ldv_check_final_state_stub();" in before
    config.preserve_ldv_property = True
    after = generator.generate_harness(parsed.get_function_info("main"), _fallback_spec("main"), parsed)
    for name in ("ldv_check_final_state", "predicate", "write_state", "driver"):
        assert f"Inlined real callee body: {name}" in after
        assert f"{name}_stub(" not in after
    assert "ldv_mutex = 2;" in after
    assert "expensive_stub();" in after
    assert "Inlined real callee body: expensive" not in after
    config.preserve_ldv_property = False
    assert generator.generate_harness(parsed.get_function_info("main"), _fallback_spec("main"), parsed) == before


def test_ldv_guard_is_opt_in_and_rejects_non_ldv_input(monkeypatch, tmp_path):
    from bmc_agent.harness_generator import _ldv_property_functions
    import pytest
    monkeypatch.delenv("BMC_AGENT_PRESERVE_LDV_PROPERTY", raising=False)
    assert Config.from_env().preserve_ldv_property is False
    monkeypatch.setenv("BMC_AGENT_PRESERVE_LDV_PROPERTY", "true")
    assert Config.from_env().preserve_ldv_property is True
    source = tmp_path / "other.c"
    source.write_text("int main(void) { return 0; }")
    with pytest.raises(ValueError, match="requires defined LDV"):
        _ldv_property_functions(parse_c_file(source))
