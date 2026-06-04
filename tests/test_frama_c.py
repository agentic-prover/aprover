"""Frama-C/WP backend — ACSL placement + WP-output parsing + graceful degradation
(everything that doesn't need the frama-c binary)."""
from bmc_agent.frama_c import (
    insert_loop_invariants_acsl, insert_contract_acsl, parse_wp_output, run_wp,
    frama_c_available, WPResult, function_assigns_nothing,
)


def test_function_assigns_nothing_pure_vs_impure():
    # pure leaf functions (no escaping store) -> assigns \nothing
    assert function_assigns_nothing("int add(int *p,int *q){ return *p + *q; }", "add")
    assert function_assigns_nothing("int max(int x,int y){ if(x>=y) return x; return y; }", "max")
    # local scalar writes don't touch the frame -> still pure
    assert function_assigns_nothing("int g(int n){ int t=0; t=n+1; return t; }", "g")
    # escaping stores -> NOT assigns \nothing
    assert not function_assigns_nothing("void set(int *p,int v){ *p = v; }", "set")
    assert not function_assigns_nothing("void z(int *a){ a[0] = 1; }", "z")
    assert not function_assigns_nothing("void s(struct T *p){ p->f = 1; }", "s")
    # comparisons / unknown function must not be misread as stores
    assert function_assigns_nothing("int cmp(int *p,int *q){ return *p == *q; }", "cmp")
    assert not function_assigns_nothing("int h(int x){ return x; }", "missing")


def test_insert_loop_invariants_acsl_before_loop():
    src = ("void main(){\n int A[8]; unsigned i;\n"
           "  for (i = 0; i < 8; i++) { A[i] = i; }\n}\n")
    out = insert_loop_invariants_acsl(
        src, {0: ["i <= 8", "forall k : 0 <= k < i ==> A[k] == k"]}, {0: "i, A[..]"})
    assert "loop invariant i <= 8;" in out
    assert "loop invariant \\forall integer k; (0 <= k < i) ==> (A[k] == k);" in out
    assert "loop assigns i, A[..];" in out
    # ACSL block precedes the for-loop
    assert out.index("loop invariant") < out.index("for (i = 0")


def test_insert_contract_acsl_before_function():
    src = "#include <x.h>\nint add(int *p, int *q) {\n return *p + *q;\n}\n"
    out = insert_contract_acsl(src, "add", requires="valid(p) && valid(q)",
                               ensures="result == *p + *q")
    assert "requires (\\valid(p)) && (\\valid(q));" in out
    assert "ensures \\result == *p + *q;" in out
    assert out.index("requires") < out.index("int add(")


def test_insert_contract_acsl_noop_when_vacuous():
    src = "int f(void){ return 0; }\n"
    assert insert_contract_acsl(src, "f", "true", "true") == src    # nothing to say


def test_parse_wp_output_summary_all_proved():
    raw = "[wp] 12 goals scheduled\n[wp] Proved goals:   12 / 12\n"
    assert parse_wp_output(raw) == (12, 12, [])


def test_parse_wp_output_summary_with_unproved():
    raw = ("[wp] Proved goals:   3 / 5\n"
           "[wp] [Alt-Ergo] typed_main_loop_invariant_preserved : Unknown\n"
           "[wp] [Alt-Ergo] typed_main_assert : Valid\n"
           "[wp] [Qed] typed_main_loop_invariant_established : Valid\n"
           "[wp] [Alt-Ergo] typed_main_assert_2 : Timeout\n")
    n_proved, n_total, unproved = parse_wp_output(raw)
    assert (n_proved, n_total) == (3, 5)
    assert "typed_main_loop_invariant_preserved" in unproved
    assert "typed_main_assert_2" in unproved
    assert "typed_main_assert" not in unproved          # Valid


def test_parse_wp_output_pergoal_fallback_no_summary():
    raw = ("[wp] [Alt-Ergo] g_assert : Valid\n"
           "[wp] [Alt-Ergo] g_loop_invariant_preserved : Failed\n")
    assert parse_wp_output(raw) == (1, 2, ["g_loop_invariant_preserved"])


def test_run_wp_not_installed_is_graceful():
    r = run_wp("int main(){return 0;}", frama_c_path="frama-c-DOES-NOT-EXIST")
    assert isinstance(r, WPResult)
    assert r.available is False
    assert "not installed" in r.error.lower()


def test_synthesize_frama_c_oracle_unavailable_exits_cleanly(tmp_path, monkeypatch):
    """--oracle frama-c with no frama-c on PATH: a clear note, no crash/loop."""
    from bmc_agent.config import Config
    from bmc_agent.loop_invariants import synthesize_loop_invariants
    import bmc_agent.frama_c as fc
    monkeypatch.setattr(fc, "frama_c_available", lambda *a, **k: False)
    f = tmp_path / "b.c"
    f.write_text("void main(){ int i; for(i=0;i<8;i++){} __VERIFIER_assert(i==8); }")
    cfg = Config(); cfg.oracle = "frama-c"

    class MockLLM:
        def complete(self, *a, **k): return "i <= 8"
    r = synthesize_loop_invariants(str(f), cfg, MockLLM(), entry="main")
    assert r.ok is False
    assert "frama-c" in r.note.lower() and "path" in r.note.lower()
