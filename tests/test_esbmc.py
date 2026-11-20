"""Backend contract tests plus optional tests against an installed ESBMC 8.3."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from bmc_agent.config import Config
from bmc_agent.cbmc import CBMCResult
from bmc_agent.c_bmc import run_c_bmc, executable_for
from bmc_agent.esbmc import _intrinsic_defines, _limits, parse_esbmc_result, run_esbmc


@pytest.fixture(autouse=True)
def clean_policy(monkeypatch):
    for key in ("SVCOMP_PROP", "SVCOMP_ARCH", "SVCOMP_UNWIND", "SVCOMP_TIMEOUT",
                "BMC_UNWIND_CAP", "BMC_CONE_SLICE", "BMC_BUGHUNT"):
        monkeypatch.delenv(key, raising=False)


def test_factory_dispatch_and_env(monkeypatch):
    from bmc_agent.backends import backend_for, ESBMCBackend, CBMCBackend
    monkeypatch.setenv("BMC_AGENT_C_BACKEND", "esbmc")
    monkeypatch.setenv("BMC_AGENT_ESBMC_PATH", "/test/esbmc")
    config = Config.from_env()
    assert isinstance(backend_for("c", config), ESBMCBackend)
    assert executable_for(config) == "/test/esbmc"
    expected = CBMCResult(False, error="test")
    with patch("bmc_agent.esbmc.run_esbmc", return_value=expected) as selected:
        assert run_c_bmc("h.c", config=config, cbmc_path="DO_NOT_RUN_CBMC") is expected
    assert selected.call_args.kwargs["esbmc_path"] == "/test/esbmc"
    assert "cbmc_path" not in selected.call_args.kwargs
    config = Config()
    assert isinstance(backend_for("c", config), CBMCBackend)
    with patch("bmc_agent.c_bmc.run_cbmc", return_value=expected) as selected:
        assert run_c_bmc("h.c", config=config) is expected
    assert selected.call_args.kwargs["cbmc_path"] == "cbmc"
    with pytest.raises(ValueError, match="Unsupported"):
        backend_for("c", Config(c_backend="typo"))


def test_cli_selection_is_scoped(monkeypatch):
    from bmc_agent.cli import main
    monkeypatch.setenv("BMC_AGENT_C_BACKEND", "cbmc")
    def check(args):
        assert Config.from_env().c_backend == "esbmc"
        return 9
    with patch("bmc_agent.cli._cmd_check", side_effect=check):
        assert main(["check", "--source", "h.c", "--driver", "test", "--c-backend", "esbmc"]) == 9
    assert os.environ["BMC_AGENT_C_BACKEND"] == "cbmc"


def test_cbmc_intrinsics_are_never_silently_dropped():
    assert "__CPROVER_assert=__ESBMC_assert" in _intrinsic_defines('__CPROVER_assert(0, "x");')
    _intrinsic_defines('int __CPROVER_custom(int a) {return a;} // __CPROVER_ignored\n')
    with pytest.raises(ValueError, match="__CPROVER_havoc_object"):
        _intrinsic_defines('void *p; __CPROVER_havoc_object(p);')
    native_malloc = _intrinsic_defines('void *malloc(unsigned n); int main(){__CPROVER_allocate(4,0);}')
    assert not any(d.startswith("malloc=") for d in native_malloc)


def test_budget_policy(monkeypatch):
    monkeypatch.setenv("SVCOMP_PROP", "unreach")
    monkeypatch.setenv("SVCOMP_UNWIND", "64")
    monkeypatch.setenv("SVCOMP_TIMEOUT", "300")
    monkeypatch.setenv("BMC_UNWIND_CAP", "1")
    assert _limits(8, 30) == (1, 300)


def witness(comment="target assertion"):
    return [{"steps": [
        {"type": "assignment", "file": "h.c", "line": "4", "function": "f",
         "assignment": {"lhs": "x", "rhs": "3"}},
        {"type": "violation", "file": "h.c", "line": "5", "function": "f",
         "assertion": {"comment": comment, "guard": "x != 3", "violated": True}},
    ]}]


def test_normalized_counterexample():
    report = witness()
    report[0]["initial_values"] = {"global_input": {"value": "7"}}
    result = parse_esbmc_result(1, "VERIFICATION FAILED", report)
    assert not result.error and not result.verified
    cex = result.counterexamples[0]
    assert cex.variable_assignments == {"x": "3", "global_input": "7"}
    assert cex.failure_location == {"file": "h.c", "line": "5", "function": "f"}
    assert cex.failing_property.startswith("f.assertion.")
    assert "x = 3" in cex.trace[0]
    unwind = parse_esbmc_result(1, "VERIFICATION FAILED", witness("unwinding assertion loop 1"))
    assert ".unwind." in unwind.counterexamples[0].failing_property
    assert unwind.counterexamples[0].failing_property.rsplit(".", 1)[-1].isdigit()


def test_ldv_target_normalization_requires_unique_assertion(tmp_path):
    from bmc_agent.esbmc import _normalize_ldv_target
    report = witness("target")
    report[0]["steps"][-1]["function"] = "reach_error"
    result = parse_esbmc_result(1, "VERIFICATION FAILED", report)
    original = result.counterexamples[0].failing_property
    source = 'void reach_error(){ __CPROVER_assert(0,"first"); __CPROVER_assert(0,"second"); }'
    _normalize_ldv_target(result, tmp_path / "ambiguous.c", source)
    assert result.counterexamples[0].failing_property == original


@pytest.mark.parametrize("code,raw,report", [
    (0, "VERIFICATION SUCCESSFUL", None),
    (0, "Generated 0 VCC(s)\nVERIFICATION SUCCESSFUL", None),
    (0, "WARNING: no body for function __CPROVER_assert\nGenerated 1 VCC(s)\nVERIFICATION SUCCESSFUL", None),
    (1, "VERIFICATION FAILED", None),
    (1, "VERIFICATION FAILED", {}),
    (1, "VERIFICATION FAILED", []),
    (-11, "VERIFICATION SUCCESSFUL", None),
    (0, "Generated 1 VCC(s)\nVERIFICATION SUCCESSFUL", witness()),
    (0, "error: use of undeclared identifier\nVERIFICATION SUCCESSFUL", None),
])
def test_fail_closed(code, raw, report):
    result = parse_esbmc_result(code, raw, report)
    assert result.error and not result.verified and not result.counterexamples


def test_supported_success():
    result = parse_esbmc_result(0, "Generated 1 VCC(s), 0 remaining\nVERIFICATION SUCCESSFUL")
    assert result.verified and not result.error


def test_assertion_message_is_not_a_frontend_error():
    result = parse_esbmc_result(1, "Starting Bounded Model Checking\nerror: custom assertion\nVERIFICATION FAILED",
                                witness("error: custom assertion"))
    assert not result.error and result.counterexamples


@pytest.mark.parametrize("option", ["conversion_check", "pointer_overflow_check", "object_bits"])
def test_unsupported_checks_are_not_ignored(option):
    result = run_esbmc("h.c", **{option: 12 if option == "object_bits" else True})
    assert "unsupported options" in result.error


def test_command_and_timeout(tmp_path):
    source = tmp_path / "h.c"
    source.write_text('int main(){__CPROVER_assert(0,"test");}')
    with patch("bmc_agent.esbmc.shutil.which", return_value="/bin/true"), patch(
        "bmc_agent.esbmc.subprocess.run", side_effect=subprocess.TimeoutExpired("esbmc", 3, output=b"partial log")
    ) as run:
        result = run_esbmc(source, unwind=2, timeout=3, arch="LP64", partial_loops=True,
                           unsigned_overflow_check=True, undefined_shift_check=True)
    command = run.call_args.args[0]
    assert "--64" in command and "--no-unwinding-assertions" in command
    assert "--unsigned-overflow-check" in command and "--ub-shift-check" in command
    assert "--no-pointer-check" in command and "--no-bounds-check" in command
    assert "--no-div-by-zero-check" in command
    assert "--k-induction" not in command
    assert result.error == "esbmc timed out after 3s"
    assert result.raw_output == "partial log"
    assert not Path(run.call_args.kwargs["cwd"]).exists()


def test_unused_void_extern_omission_is_narrow():
    from bmc_agent.esbmc import _omit_unused_void_externs

    source = ('extern void unused;\nextern void called(void);\n'
              'extern void *pointer;\nextern void array[];\nint main(void){return 0;}\n')
    rewritten, omitted = _omit_unused_void_externs(source, [])
    assert omitted == ["unused"]
    assert rewritten == " " * len("extern void unused;") + source[len("extern void unused;"):]
    assert len(rewritten) == len(source) and rewritten.count("\n") == source.count("\n")


@pytest.mark.parametrize("reference", [
    'int main(void){return &symbol != 0;}',
    'const char *name = "symbol";',
    '__asm__(".globl symbol");',
    '#define JOIN(a,b) a ## b\nint main(void){return &JOIN(sym,bol) != 0;}',
    '#include "references.h"',
])
def test_referenced_or_unprovable_void_extern_is_preserved(reference):
    from bmc_agent.esbmc import _omit_unused_void_externs

    source = 'extern void symbol;\n' + reference + '\n'
    assert _omit_unused_void_externs(source, []) == (source, [])
    assert _omit_unused_void_externs('extern void symbol;\n', ['ALIAS=symbol']) == (
        'extern void symbol;\n', [])


def test_void_extern_copy_is_ldv_only_and_preserves_input_and_flags(tmp_path):
    source = tmp_path / "h.c"
    original = 'extern void unused;\nint main(void){return 0;}\n'
    source.write_text(original)
    commands = []

    def solver(command, **kwargs):
        commands.append(command.copy())
        if len(commands) == 1:
            assert command[1] == str(source)
        else:
            assert command[1] != str(source)
            assert Path(command[1]).read_text() == ' ' * len('extern void unused;') + original[len('extern void unused;'):]
        return subprocess.CompletedProcess(command, 0, 'Generated 1 VCC(s)\nVERIFICATION SUCCESSFUL', '')

    with patch("bmc_agent.esbmc.shutil.which", return_value="/bin/true"), patch(
            "bmc_agent.esbmc.subprocess.run", side_effect=solver):
        run_esbmc(source, apply_env_policy=False)
        run_esbmc(source, preserve_ldv_property=True, apply_env_policy=False,
                  evidence_dir=tmp_path / 'evidence')
    assert commands[0][2:] == commands[1][2:]
    assert commands[1][commands[1].index('-I') + 1] == str(tmp_path)
    assert source.read_text() == original
    evidence = json.loads((tmp_path / 'evidence/invocation.json').read_text())
    assert evidence['omitted_unused_void_externs'] == ['unused']
    assert (tmp_path / 'evidence/solver_input.c').is_file()


ESBMC = os.environ.get("TEST_ESBMC_PATH") or shutil.which("esbmc")
real_esbmc = pytest.mark.skipif(not ESBMC, reason="set TEST_ESBMC_PATH to run installed-backend tests")


@real_esbmc
def test_real_ldv_unused_void_extern_compatibility(tmp_path):
    source = tmp_path / "void_extern.c"
    original = ('extern void unused_signal;\nextern void unused_rt_signal;\n'
                'void __ESBMC_assert(_Bool, const char *);\n'
                'int main(void){__ESBMC_assert(0,"target");return 0;}\n')
    source.write_text(original)
    options = dict(esbmc_path=ESBMC, unwind=1, timeout=15, arch='ILP32',
                   partial_loops=True, apply_env_policy=False)
    unchanged = run_esbmc(source, **options)
    assert unchanged.error and 'symbolic_type_excp' in unchanged.raw_output
    result = run_esbmc(source, preserve_ldv_property=True, **options)
    assert not result.error and [c.description for c in result.counterexamples] == ['target']
    assert source.read_text() == original


@real_esbmc
def test_real_assume_assert_and_parallel_reports(tmp_path):
    def check(value):
        source = tmp_path / f"h{value}.c"
        source.write_text(f'int nondet_int(void); int main(){{int x=nondet_int(); '
                          f'__CPROVER_assume(x=={value}); __CPROVER_assert(x!={value},"target {value}");}}')
        return run_esbmc(source, esbmc_path=ESBMC, timeout=10, evidence_dir=tmp_path / str(value))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(check, (3, 7)))
    for value, result in zip((3, 7), results):
        assert not result.error and not result.verified
        assert result.counterexamples[0].variable_assignments["x"] == str(value)
        assert result.counterexamples[0].description == f"target {value}"
        assert (tmp_path / str(value) / "report.json").is_file()


@real_esbmc
def test_real_safe_and_unwind_failure(tmp_path):
    source = tmp_path / "safe.c"
    source.write_text('int nondet_int(void); int main(){int x=nondet_int(); '
                      '__CPROVER_assume(x==3); __CPROVER_assert(x==3,"safe");}')
    result = run_esbmc(source, esbmc_path=ESBMC, timeout=10)
    assert result.verified and not result.error
    source.write_text('int main(){int i=0; while(i<10) i++; __CPROVER_assert(i==10,"done");}')
    result = run_esbmc(source, esbmc_path=ESBMC, unwind=1, timeout=10)
    assert not result.error and not result.verified
    assert any(".unwind." in c.failing_property for c in result.counterexamples)


@real_esbmc
@pytest.mark.parametrize("source,options,description", [
    ('int nondet_int(void); int main(){int x=nondet_int(); __CPROVER_assume(x==2147483647); return x+1;}',
     {"signed_overflow_check": True}, "overflow"),
    ('unsigned nondet_uint(void); int main(){unsigned x=nondet_uint(); __CPROVER_assume(x==4294967295U); return x+1;}',
     {"unsigned_overflow_check": True}, "overflow"),
    ('int main(){int a[2]; return a[2];}', {"bounds_check": True}, "bounds"),
    ('int main(){int *p=0; return *p;}', {"pointer_check": True}, "pointer"),
    ('int nondet_int(void); int main(){int x=nondet_int(); return 4/x;}', {"div_by_zero_check": True}, "zero"),
    ('int nondet_int(void); int main(){int x=nondet_int(); __CPROVER_assume(x==32); return 1<<x;}',
     {"undefined_shift_check": True}, "shift"),
])
def test_real_check_flags(tmp_path, source, options, description):
    path = tmp_path / "flags.c"
    path.write_text(source)
    result = run_esbmc(path, esbmc_path=ESBMC, timeout=10, **options)
    assert not result.error and not result.verified, result.error
    assert any(description in c.description.lower() for c in result.counterexamples)
    kind = {"zero": "div-by-zero", "pointer": "pointer_dereference"}.get(description, description)
    assert any(f".{kind}." in c.failing_property for c in result.counterexamples)


@real_esbmc
def test_real_included_assertion_and_unknown_intrinsic(tmp_path):
    (tmp_path / "checks.h").write_text('void check(){__CPROVER_assert(0,"header assertion");}')
    path = tmp_path / "h.c"
    path.write_text('#include "checks.h"\nint main(){check();}')
    result = run_esbmc(path, esbmc_path=ESBMC, timeout=10)
    assert not result.error and result.counterexamples[0].description == "header assertion"
    (tmp_path / "checks.h").write_text('int __CPROVER_unsupported(void); void check(){__CPROVER_unsupported();}')
    result = run_esbmc(path, esbmc_path=ESBMC, timeout=10)
    assert result.error and not result.verified


@real_esbmc
def test_engine_and_refinement_guard_use_esbmc_without_cbmc_or_llm(tmp_path):
    from unittest.mock import MagicMock
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.bmc_engine import BMCEngine
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.cbmc import Counterexample
    source = tmp_path / "example.c"
    source.write_text('int f(int x){return x;}')
    parsed = parse_c_file(source)
    func = parsed.get_function_info("f")
    config = Config(c_backend="esbmc", esbmc_path=ESBMC, cbmc_path="DO_NOT_RUN_CBMC",
                    artifact_dir=str(tmp_path / "out"), enable_agentic_harness_repair=False)
    store = ArtifactStore(config.artifact_dir)
    engine = BMCEngine(config, store)
    spec = Spec(function_name="f", precondition="x == 3", postcondition="result != 3")
    llm = MagicMock()
    llm.complete.side_effect = AssertionError("this test must never call an LLM")
    with patch("bmc_agent.c_bmc.run_cbmc", side_effect=AssertionError("CBMC must not run")):
        result = engine.check_function(func, spec, parsed, "example")
        assert not result.error and result.counterexamples
        validator = CExValidator(config, llm, store, HarnessGenerator(config))
        # x=0 is excluded as the original spurious state. x=1 remains a witness
        # showing that x>=2 removes valid states allowed by the old x>=0 spec.
        guard = validator._check_over_refinement_with_cbmc(
            "x >= 2", "x >= 0", Counterexample("f.assertion.1", {"x": "0"}), func)
        assert guard is False
    llm.complete.assert_not_called()


@real_esbmc
def test_agentic_compile_check_uses_selected_frontend(tmp_path):
    from bmc_agent.agentic_harness_gen import AgenticHarnessGen
    config = Config(c_backend="esbmc", esbmc_path=ESBMC, cbmc_path="DO_NOT_RUN_CBMC")
    gen = AgenticHarnessGen(config, {}, tmp_path)
    gen._include_dirs, gen._defines = [], []
    ok, error = gen._compile_check('int main(){__CPROVER_assert(0,"target");}')
    assert ok and not error


def test_replay_requires_matching_source_and_explicit_configuration(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("replay_esbmc", Path(__file__).parents[1] / "scripts/replay_esbmc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "h.c"
    path.write_text("int main() {}")
    with pytest.raises(ValueError, match="hash"):
        module.match_invocation(path, [{"harness_sha256": "stale"}])
    options = module.recorded_options(["cbmc", "h.c", "--json-ui", "--unwind", "1",
                                        "--32", "--partial-loops", "--refine", "--slice-formula"])
    assert options["unwind"] == 1 and options["arch"] == "ILP32"
    assert options["partial_loops"] and not options["apply_env_policy"]
    with pytest.raises(ValueError, match="unmapped"):
        module.recorded_options(["cbmc", "h.c", "--unknown-option"])

    import hashlib
    record = {"command": ["cbmc", str(path), "--json-ui", "--unwind", "1",
                          "--partial-loops", "--object-bits", "12"],
              "harness_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    invocations = tmp_path / "invocations.jsonl"
    invocations.write_text(json.dumps(record) + "\n")
    with patch.object(module, "version", return_value="test version"), patch.object(
        module, "run_esbmc", return_value=CBMCResult(True)
    ) as run:
        assert module.main(["--harness", str(path), "--invocations", str(invocations),
                            "--output", str(tmp_path / "replay"),
                            "--allow-object-encoding-change"]) == 0
    assert "object_bits" not in run.call_args.kwargs
    summary = json.loads((tmp_path / "replay/summary.json").read_text())
    assert summary["cases"][0]["recorded_options"]["object_bits"] == 12
    assert summary["cases"][0]["encoding_difference"]["cbmc_object_bits"] == 12
    assert summary["llm_calls"] == 0


@real_esbmc
def test_real_bounds_feasibility_preserves_original_check(tmp_path):
    from unittest.mock import MagicMock
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.cbmc import Counterexample
    source = tmp_path / "array.c"
    source.write_text('int read_slot(unsigned slot){int a[4]={1,2,3,4}; '
                      'if(slot>4)return -1; return a[slot];}\n'
                      'int entry(unsigned slot){return read_slot(slot);}')
    parsed = parse_c_file(source)
    config = Config(c_backend="esbmc", esbmc_path=ESBMC, cbmc_path="DO_NOT_RUN_CBMC",
                    artifact_dir=str(tmp_path / "out"))
    gen = HarnessGenerator(config)
    validator = CExValidator(config, MagicMock(), ArtifactStore(config.artifact_dir), gen)
    specs = {name: Spec(function_name=name, precondition="true", postcondition="true")
             for name in ("entry", "read_slot")}
    ce = Counterexample("read_slot.bounds.1", {"slot": "4"}, description="array bounds violated")
    result = validator._check_cex_feasibility(parsed.get_function_info("entry"), specs["entry"],
                                             ce, parsed, specs)
    assert result is True
    # Do not turn an unwinding-only failure into confirmation of a bounds bug.
    with patch("bmc_agent.cex_validator.run_cbmc", return_value=CBMCResult(
        False, counterexamples=[Counterexample("entry.unwind.1")])):
        assert validator._check_cex_feasibility(parsed.get_function_info("entry"), specs["entry"],
                                                ce, parsed, specs) is None
        assert validator._feas_errored


@real_esbmc
def test_real_guarded_ldv_feasibility_keeps_target_and_effective_unwind(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    source = tmp_path / "ldv.c"
    text = ('void reach_error(void){__CPROVER_assert(0,"target");}\n'
            'void callee(void){for(int i=0;i<2;i++)if(i==1)reach_error();}\n'
            'int main(void){callee();return 0;}\n')
    source.write_text(text)
    for key, value in {"SVCOMP_PROP": "unreach", "SVCOMP_UNWIND": "0",
                       "SVCOMP_TIMEOUT": "0", "BMC_BUGHUNT": "1"}.items():
        monkeypatch.setenv(key, value)
    config = Config(c_backend="esbmc", esbmc_path=ESBMC, cbmc_path="DO_NOT_RUN_CBMC",
                    preserve_ldv_property=True, cbmc_unwind=1, cbmc_timeout=10,
                    effective_unwind_by_function={"main": 2}, artifact_dir=str(tmp_path / "out"))
    parsed = parse_c_file(source)
    with patch("bmc_agent.c_bmc.run_cbmc", side_effect=AssertionError("CBMC must not run")):
        primary = run_c_bmc(source, config=config, unwind=2)
        assert not primary.error
        target = next(c for c in primary.counterexamples if c.failing_property == "reach_error.assertion.1")
        generator = HarnessGenerator(config)
        validator = CExValidator(config, MagicMock(), ArtifactStore(config.artifact_dir), generator)
        spec = Spec(function_name="main", precondition="true", postcondition="true")
        with patch.object(generator, "generate_feasibility_harness", return_value="\n\n" + text), patch(
            "bmc_agent.esbmc.run_esbmc", wraps=run_esbmc) as solver:
            assert validator._check_cex_feasibility(parsed.get_function_info("main"), spec, target, parsed) is True
        assert solver.call_args.kwargs["unwind"] == 2
        assert "plain_output" not in solver.call_args.kwargs and "use_refine" not in solver.call_args.kwargs
        assert validator._feas_evidence["attempts"][0]["mode"] == "esbmc-json"
        unrelated = text.replace("callee();return 0;", '__CPROVER_assert(0,"different");return 0;')
        with patch.object(generator, "generate_feasibility_harness", return_value=unrelated):
            assert validator._check_cex_feasibility(parsed.get_function_info("main"), spec, target, parsed) is None


@real_esbmc
def test_real_nonzeroed_allocation_preserves_benchmark_malloc(tmp_path):
    source = tmp_path / "allocate.c"
    source.write_text('typedef __SIZE_TYPE__ size_t;\n'
                      'void *malloc(size_t n){return 0;}\n'
                      'int main(){void *(*original_malloc)(size_t)=malloc;\n'
                      'char *p=__CPROVER_allocate(4,0); char *q=__CPROVER_allocate(4,0);\n'
                      '__CPROVER_assert(original_malloc(4)==0,"original allocator");\n'
                      '__CPROVER_assert(p!=0 && q!=0 && p!=q,"fresh nonnull objects");\n'
                      'p[3]=17; __CPROVER_assert(p[3]==17,"writable extent");\n'
                      '__CPROVER_assert(q[0]==0,"not initialized to zero");}\n')
    result = run_esbmc(source, esbmc_path=ESBMC, timeout=10, pointer_check=True, bounds_check=True)
    assert not result.error
    assert [c.description for c in result.counterexamples] == ["not initialized to zero"]
    source.write_text('int main(){void *p=__CPROVER_allocate(4,1);}')
    result = run_esbmc(source, esbmc_path=ESBMC, timeout=10)
    assert result.error and "only nonzeroed allocation" in result.error


def test_kernel_feasibility_header_change_is_esbmc_only(tmp_path):
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.cbmc import Counterexample
    source = tmp_path / "kernel.c"
    source.write_text('typedef unsigned long __kernel_size_t; typedef __kernel_size_t size_t;\n'
                      'typedef unsigned int __kernel_dev_t; typedef __kernel_dev_t dev_t;\n'
                      'void callee(void){} int main(){callee();return 0;}')
    parsed = parse_c_file(source)
    kwargs = dict(func=parsed.get_function_info('main'), parsed_file=parsed,
                  spec=Spec(function_name='main', precondition='true', postcondition='true'),
                  counterexample=Counterexample('reach_error.assertion.1'))
    cbmc = HarnessGenerator(Config(preserve_ldv_property=True)).generate_feasibility_harness(**kwargs)
    cbmc_plain = HarnessGenerator(Config()).generate_feasibility_harness(**kwargs)
    esbmc_plain = HarnessGenerator(Config(c_backend='esbmc')).generate_feasibility_harness(**kwargs)
    esbmc = HarnessGenerator(Config(c_backend='esbmc', preserve_ldv_property=True)).generate_feasibility_harness(**kwargs)
    assert '#include <stddef.h>' in cbmc
    assert cbmc_plain == cbmc
    assert esbmc_plain == cbmc.replace('#include <stddef.h>\n', '')
    assert esbmc == esbmc_plain.replace('#include <assert.h>\n', '').replace('#include <stdint.h>', '')


@pytest.mark.parametrize('body, retained', [
    ('uint32_t missing_type;', ['#include <stdint.h>']),
    ('uintptr_t x = UINTPTR_MAX;', ['#include <stdint.h>']),
    ('assert(0);', ['#include <assert.h>']),
    ('#include "unknown.h"', ['#include <assert.h>', '#include <stdint.h>']),
])
def test_feasibility_headers_keep_actual_or_unknown_dependencies(body, retained):
    from bmc_agent.harness_generator import _self_contained_feasibility_headers

    declarations = 'typedef unsigned long uintptr_t;'
    assert _self_contained_feasibility_headers(
        ['#include <assert.h>', '#include <stdint.h>'], declarations + body, declarations,
    ) == retained


def test_feasibility_header_member_is_not_a_typedef():
    from bmc_agent.harness_generator import _self_contained_feasibility_headers

    declarations = 'typedef struct { int uint32_t; } metadata;'
    assert _self_contained_feasibility_headers(
        ['#include <stdint.h>'], declarations + '\nuint32_t value;', declarations,
    ) == ['#include <stdint.h>']


def test_feasibility_header_check_includes_generated_postcondition(tmp_path):
    from bmc_agent.harness_generator import HarnessGenerator
    from bmc_agent.parser import parse_c_file
    from bmc_agent.spec import Spec
    from bmc_agent.cbmc import Counterexample

    source = tmp_path / 'kernel.c'
    source.write_text('typedef unsigned int __kernel_dev_t; typedef __kernel_dev_t dev_t;\n'
                      'typedef unsigned long uintptr_t; int work(void){return 0;}')
    parsed = parse_c_file(source)
    harness = HarnessGenerator(Config(c_backend='esbmc', preserve_ldv_property=True)).generate_feasibility_harness(
        func=parsed.get_function_info('work'), parsed_file=parsed,
        spec=Spec('work', 'true', 'result >= 0'), counterexample=Counterexample('work.assertion.1'))
    assert '#include <assert.h>' in harness and 'assert(result >= 0)' in harness
    assert '#include <stdint.h>' not in harness
    assert 'typedef unsigned long uintptr_t;' in harness
