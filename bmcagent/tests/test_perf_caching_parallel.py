"""(a) prompt-cache wiring in the tool loop + (b) parallel spec-gen within layers."""
from bmc_agent.llm import _supports_explicit_prompt_cache, _system_msg_with_cache


# --- (a) prompt caching ------------------------------------------------------

def test_explicit_cache_only_for_anthropic_openrouter():
    assert _supports_explicit_prompt_cache("https://openrouter.ai/api/v1")
    assert _supports_explicit_prompt_cache("https://api.anthropic.com")
    assert not _supports_explicit_prompt_cache("https://api.openai.com/v1")
    assert not _supports_explicit_prompt_cache("")


def test_system_msg_cache_block_vs_plain():
    m = _system_msg_with_cache("SYS", "https://openrouter.ai/api/v1")
    assert isinstance(m["content"], list)
    assert m["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert m["content"][0]["text"] == "SYS"
    m2 = _system_msg_with_cache("SYS", "https://api.openai.com/v1")
    assert m2["content"] == "SYS"          # plain string -> OpenAI auto-prefix-cache


# --- (b) parallel spec-gen within a layer -----------------------------------

def test_generate_specs_parallel_within_layer(tmp_path, monkeypatch):
    from bmc_agent.spec_generator_v2 import SpecGeneratorV2
    from bmc_agent.spec import Spec
    from bmc_agent.config import Config
    import threading

    src = tmp_path / "m.c"
    # three independent functions -> one bottom-up layer -> generated in parallel
    src.write_text("int a(void){return 1;}\nint b(void){return 2;}\nint c(void){return 3;}\n")

    g = SpecGeneratorV2.__new__(SpecGeneratorV2)
    g.config = Config()
    g.store = type("S", (), {
        "save_spec": lambda self, *a, **k: None,
        "init_driver": lambda self, *a, **k: None,
    })()
    g.boundary_detector = None
    g.corpus_paths = [src]
    g.k_callers = 5
    g._spec_system_prompt = "x"

    seen = set()
    lock = threading.Lock()

    def fake_gen(func_info, parsed, all_specs_so_far, corpus_paths):
        with lock:
            seen.add(func_info.name)
        return Spec(function_name=func_info.name, precondition="true", postcondition="true")

    monkeypatch.setattr(g, "_generate_one", fake_gen)
    specs = g.generate_specs(str(src), "drv")

    # all functions generated, none lost in the parallel layer
    assert {"a", "b", "c"} <= set(specs.keys())
    assert {"a", "b", "c"} <= seen


# --- (c) only_functions prunes LLM spec-gen to target + transitive callees ---

def test_generate_specs_prunes_to_target_and_callees(tmp_path, monkeypatch):
    from bmc_agent.spec_generator_v2 import SpecGeneratorV2
    from bmc_agent.spec import Spec
    from bmc_agent.config import Config
    import threading

    src = tmp_path / "m.c"
    # caller -> target -> leaf ; plus an unrelated function `other`.
    # Checking `target` needs specs for target + leaf only. `caller` (a CALLER
    # of target) and `other` (unrelated) must NOT trigger an LLM call.
    src.write_text(
        "int leaf(void){return 1;}\n"
        "int target(void){return leaf();}\n"
        "int caller(void){return target();}\n"
        "int other(void){return 7;}\n"
    )

    g = SpecGeneratorV2.__new__(SpecGeneratorV2)
    g.config = Config()
    g.store = type("S", (), {
        "save_spec": lambda self, *a, **k: None,
        "init_driver": lambda self, *a, **k: None,
    })()
    g.boundary_detector = None
    g.corpus_paths = [src]
    g.k_callers = 5
    g._spec_system_prompt = "x"

    seen = set()
    lock = threading.Lock()

    def fake_gen(func_info, parsed, all_specs_so_far, corpus_paths):
        with lock:
            seen.add(func_info.name)
        return Spec(function_name=func_info.name, precondition="true", postcondition="true")

    monkeypatch.setattr(g, "_generate_one", fake_gen)
    specs = g.generate_specs(str(src), "drv", only_functions={"target"})

    # returned dict is still complete (pipeline contract unchanged)
    assert {"leaf", "target", "caller", "other"} <= set(specs.keys())
    # but only target + its transitive callee got an actual LLM round-trip
    assert seen == {"target", "leaf"}
    assert "caller" not in seen and "other" not in seen
