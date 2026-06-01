"""Tests for the caller-grounded soundness gate on refinement."""
from bmc_agent.agents.soundness import SoundnessAgent, SoundnessVerdict, _extract_json
from bmc_agent.pipeline import _cited_caller_is_fabricated


# --- SoundnessAgent.parse ---------------------------------------------------

def _parse(s):
    # parse() is pure; instantiate without triggering __init__ validation by
    # using a throwaway config/llm is unnecessary — call the unbound method.
    return SoundnessAgent.parse(SoundnessAgent.__new__(SoundnessAgent), s)


def test_parse_unsound():
    v = _parse('{"verdict":"UNSOUND","implicated_caller":"ucl_util.c:2362",'
               '"rationale":"empty string path"}')
    assert v is not None and v.verdict == "UNSOUND"
    assert v.is_unsound and not v.is_sound
    assert v.implicated_caller == "ucl_util.c:2362"


def test_parse_sound_and_unknown():
    s = _parse('{"verdict":"SOUND","rationale":"all callers guard it"}')
    assert s.verdict == "SOUND" and s.is_sound and not s.is_unsound
    u = _parse('{"verdict":"UNKNOWN"}')
    assert u.verdict == "UNKNOWN" and not u.is_unsound and not u.is_sound


def test_parse_fenced_and_prose_embedded():
    fenced = _parse('```json\n{"verdict":"SOUND"}\n```')
    assert fenced and fenced.verdict == "SOUND"
    prose = _parse('Here is my answer: {"verdict":"UNSOUND"} done.')
    assert prose and prose.verdict == "UNSOUND"


def test_parse_rejects_garbage_and_bad_verdict():
    assert _parse("no json here") is None
    assert _parse('{"verdict":"MAYBE"}') is None
    assert _parse("") is None


def test_extract_json_helper():
    assert _extract_json('{"a":1}') == {"a": 1}
    assert _extract_json("nope") is None


# --- fabricated-caller guard ------------------------------------------------

SRC = "/tmp/oss_fuzz_corpora/libucl/src/ucl_parser.c"


def test_fabricated_guard_function_name_or_empty_is_trusted():
    # No file token to falsify -> not fabricated (trust the verdict).
    assert _cited_caller_is_fabricated("", SRC) is False
    assert _cited_caller_is_fabricated("ucl_lex_number", SRC) is False


def test_fabricated_guard_nonexistent_file(tmp_path):
    src = tmp_path / "mod.c"
    src.write_text("int f(void){return 0;}\n")
    # cites a .c file that isn't in the tree -> fabricated
    assert _cited_caller_is_fabricated("ghost_lexer.c:42", str(src)) is True


def test_fabricated_guard_real_sibling_file(tmp_path):
    src = tmp_path / "a.c"
    src.write_text("int f(void){return 0;}\n")
    (tmp_path / "b.c").write_text("int g(void){return 0;}\n")
    # cites a real sibling -> not fabricated
    assert _cited_caller_is_fabricated("b.c:3 (g)", str(src)) is False


# --- CLI / config flag wiring ----------------------------------------------

def test_enable_soundness_gate_flag(monkeypatch):
    monkeypatch.delenv("BMC_AGENT_ENABLE_SOUNDNESS_GATE", raising=False)
    from bmc_agent.cli import build_parser
    from bmc_agent.config import Config
    args = build_parser().parse_args(
        ["verify", "--source", "x.c", "--driver", "d", "--enable-soundness-gate"])
    assert args.enable_soundness_gate is True
    # default off
    args2 = build_parser().parse_args(["verify", "--source", "x.c", "--driver", "d"])
    assert args2.enable_soundness_gate is False
    assert Config().enable_soundness_gate is False


def test_enable_soundness_gate_env(monkeypatch):
    monkeypatch.setenv("BMC_AGENT_ENABLE_SOUNDNESS_GATE", "1")
    from bmc_agent.config import Config
    assert Config.from_env().enable_soundness_gate is True


# --- cex_validator over-refinement guard: agentic upgrade -------------------

class _FakeAgentResult:
    def __init__(self, output):
        self.output = output
        self.error = None
    @property
    def ok(self):
        return self.output is not None


def _validator(enable_gate):
    from bmc_agent.cex_validator import CExValidator
    v = CExValidator.__new__(CExValidator)   # bypass __init__
    class _Cfg: pass
    cfg = _Cfg(); cfg.enable_soundness_gate = enable_gate
    v.config = cfg
    v.llm = None
    return v


class _Func:
    def __init__(self, source_file):
        self.name = "f"
        self.source_file = source_file


def _patch_agent(monkeypatch, verdict, caller=""):
    from bmc_agent.agents.soundness import SoundnessVerdict
    out = SoundnessVerdict(verdict=verdict, implicated_caller=caller, rationale="r")
    class _FakeAgent:
        def __init__(self, *a, **k): pass
        def run(self, **k): return _FakeAgentResult(out)
    monkeypatch.setattr("bmc_agent.agents.soundness.SoundnessAgent", _FakeAgent)


def test_guard_disabled_returns_none():
    v = _validator(enable_gate=False)
    assert v._agentic_soundness_guard(func=_Func("/x/a.c"), new_precondition="p", counterexample=None) is None


def test_guard_sound_is_safe(monkeypatch):
    _patch_agent(monkeypatch, "SOUND")
    v = _validator(enable_gate=True)
    assert v._agentic_soundness_guard(func=_Func("/x/a.c"), new_precondition="p", counterexample=None) is True


def test_guard_unsound_real_caller_is_unsafe(monkeypatch, tmp_path):
    src = tmp_path / "a.c"; src.write_text("int f(void){return 0;}\n")
    (tmp_path / "caller.c").write_text("void g(void){}\n")
    _patch_agent(monkeypatch, "UNSOUND", caller="caller.c:3")
    v = _validator(enable_gate=True)
    # real cited caller -> trust the UNSOUND -> not safe (False)
    assert v._agentic_soundness_guard(func=_Func(str(src)), new_precondition="p", counterexample=None) is False


def test_guard_unsound_fabricated_caller_defers(monkeypatch, tmp_path):
    src = tmp_path / "a.c"; src.write_text("int f(void){return 0;}\n")
    _patch_agent(monkeypatch, "UNSOUND", caller="ghost_lexer.c:42")
    v = _validator(enable_gate=True)
    # fabricated cited caller -> don't trust -> defer (None)
    assert v._agentic_soundness_guard(func=_Func(str(src)), new_precondition="p", counterexample=None) is None


def test_guard_unknown_defers(monkeypatch):
    _patch_agent(monkeypatch, "UNKNOWN")
    v = _validator(enable_gate=True)
    assert v._agentic_soundness_guard(func=_Func("/x/a.c"), new_precondition="p", counterexample=None) is None
