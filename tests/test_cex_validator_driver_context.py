from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_C = REPO_ROOT / "examples" / "simple_driver.c"


def test_driver_lifecycle_reproducer_uses_context_packet_without_llm() -> None:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import CExValidator
    from bmc_agent.config import Config
    from bmc_agent.parser import parse_c_file

    parsed = parse_c_file(str(EXAMPLE_C))
    parsed.llm_context_packet = {
        "case_id": "rockchip-demo",
        "variant": "buggy",
        "candidate_properties": [
            {
                "id": "contract:spi-controller-devm-manual-unregister",
                "property": "manual unregister of devm-managed spi controller",
                "confidence": "high",
                "evidence": {
                    "spi_unregister_controller": [
                        {"function": "rockchip_sfc_remove", "line": 1, "snippet": "spi_unregister_controller(host);"}
                    ]
                },
            }
        ],
        "kernel_api_contracts": [
            {
                "id": "spi-controller-devm-manual-unregister",
                "status": "candidate_triggered",
                "matched_apis": ["devm_spi_register_controller", "spi_unregister_controller"],
            }
        ],
    }
    cex = Counterexample(
        failing_property="bmc_agent_spi_unregister_controller.assertion.1",
        variable_assignments={f"v{i}": str(i) for i in range(200)},
        trace=[],
    )
    llm = MagicMock()
    validator = object.__new__(CExValidator)
    validator.config = Config()
    validator.llm = llm

    reproducer = validator._generate_system_entry_reproducer(
        call_chain=["bmc_agent_lifecycle_rockchip_sfc_probe_then_rockchip_sfc_remove"],
        counterexample=cex,
        all_funcs={},
        parsed_file=parsed,
    )

    assert "host C reproducer skipped for kernel-driver lifecycle harness" in reproducer
    assert "manual unregister of devm-managed spi controller" in reproducer
    assert "devm_spi_register_controller" in reproducer
    llm.complete.assert_not_called()


def test_reproducer_state_is_budgeted() -> None:
    from bmc_agent.cbmc import Counterexample
    from bmc_agent.cex_validator import _format_reproducer_state

    cex = Counterexample(
        failing_property="demo.assertion.1",
        variable_assignments={f"v{i}": "A" * 100 for i in range(50)},
        trace=[],
    )

    state = _format_reproducer_state(cex, max_entries=5, max_chars=400)

    assert "v0" in state
    assert "omitted" in state
    assert len(state) < 700


def test_kernel_driver_prelude_models_spi_devm_teardown_order() -> None:
    import importlib.util
    import sys

    script = (
        REPO_ROOT
        / "experiments"
        / "linux_driver_kernel_mode"
        / "kernel_driver_mode.py"
    )
    spec = importlib.util.spec_from_file_location("kernel_driver_mode_test_mod", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    discovery = mod.Discovery(
        package="pkg",
        source_i="source.i",
        analysis_source="analysis.c",
        driver_functions=[],
        entrypoints=[],
        driver_closure=[],
        external_symbols=[
            "devm_spi_register_controller",
            "pm_runtime_disable",
            "clk_disable_unprepare",
            "free_irq",
            "dma_free_coherent",
            "dma_unmap_single_attrs",
        ],
    )

    prelude = mod.kernel_env_prelude(discovery)
    macros = mod.kernel_contract_macro_block()

    assert "teardown before unregister of devm-managed spi controller" in prelude
    assert "bmc_agent_spi_devm_dependency_teardown" in prelude
    assert "struct usb_device *__intf_to_usbdev(struct usb_interface *intf);" in prelude
    assert "return (struct usb_device *)bmc_agent_valid_alloc" not in prelude
    assert "string copy source is non-NULL" in prelude
    assert "bmc_agent_is_err" in prelude
    assert "#define pm_runtime_disable" in macros
    assert "#define dma_free_coherent" in macros
    assert "#define sized_strscpy" in macros


def test_kernel_driver_prelude_does_not_shadow_existing_kernel_helpers() -> None:
    import importlib.util
    import sys

    script = (
        REPO_ROOT
        / "experiments"
        / "linux_driver_kernel_mode"
        / "kernel_driver_mode.py"
    )
    spec = importlib.util.spec_from_file_location("kernel_driver_mode_test_mod", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    discovery = mod.Discovery(
        package="pkg",
        source_i="source.i",
        analysis_source="analysis.c",
        driver_functions=[],
        entrypoints=[],
        driver_closure=[],
        external_symbols=["ERR_PTR", "PTR_ERR", "IS_ERR", "IS_ERR_OR_NULL", "PTR_ERR_OR_ZERO"],
    )
    source_text = """
static inline __attribute__((__gnu_inline__)) bool __attribute__((__warn_unused_result__)) IS_ERR( const void *ptr)
{
    return false;
}
static inline void *ERR_PTR(long error)
{
    return (void *)error;
}
"""

    prelude = mod.kernel_env_prelude(discovery, source_text=source_text)

    assert "int IS_ERR(const void *ptr)" not in prelude
    assert "void *ERR_PTR(long error)" not in prelude
    assert "long PTR_ERR(const void *ptr)" in prelude
    assert "int IS_ERR_OR_NULL(const void *ptr)" in prelude
    assert "bmc_agent_is_err" in prelude


def test_kernel_driver_triage_signature_includes_failure_source_line(tmp_path: Path) -> None:
    import importlib.util
    import sys

    script = (
        REPO_ROOT
        / "experiments"
        / "linux_driver_kernel_mode"
        / "kernel_driver_mode.py"
    )
    spec = importlib.util.spec_from_file_location("kernel_driver_mode_sig_test_mod", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    src = tmp_path / "harness.c"
    src.write_text(
        "int f(void) {\n"
        "  if (!adis->ops->write && !adis->ops->read)\n"
        "    return 0;\n"
        "  else if (!adis->ops->write || !adis->ops->read)\n"
        "    return -22;\n"
        "}\n",
        encoding="utf-8",
    )
    common = {
        "failing_property": "adis_init.pointer_dereference.43",
        "description": "dereference failure: pointer NULL in adis->ops->write",
        "failure_location": {"file": str(src), "function": "adis_init", "line": "2"},
    }
    after_guard = {
        **common,
        "failure_location": {"file": str(src), "function": "adis_init", "line": "4"},
    }

    assert mod.failure_source_line(common) == "if (!adis->ops->write && !adis->ops->read)"
    assert mod.failure_source_line(after_guard) == "else if (!adis->ops->write || !adis->ops->read)"
    assert mod.cex_signature("adis_init", common) != mod.cex_signature("adis_init", after_guard)

    invalid_pointer = {
        **common,
        "description": "dereference failure: pointer invalid in adis->ops->write",
        "failing_property": "adis_init.pointer_dereference.44",
    }
    candidates = [
        {
            "case": "adis",
            "variant": "buggy",
            "function": "adis_init",
            "score": 95,
            "reason": "mode:paired,safety-property:pointer_dereference,buggy-only-vs-fixed",
            "description": common["description"],
            "failing_property": common["failing_property"],
            "signature": mod.cex_signature("adis_init", common),
            "root_signature": mod.cex_root_signature("adis_init", common),
        },
        {
            "case": "adis",
            "variant": "buggy",
            "function": "adis_init",
            "score": 95,
            "reason": "mode:paired,safety-property:pointer_dereference,buggy-only-vs-fixed",
            "description": invalid_pointer["description"],
            "failing_property": invalid_pointer["failing_property"],
            "signature": mod.cex_signature("adis_init", invalid_pointer),
            "root_signature": mod.cex_root_signature("adis_init", invalid_pointer),
        },
    ]

    deduped = mod.dedupe_candidate_roots(candidates)
    assert len(deduped) == 1
    assert deduped[0]["failing_property"] == "adis_init.pointer_dereference.43"


def test_realism_driver_context_explains_lifecycle_wrappers() -> None:
    from bmc_agent.realism_checker import _format_driver_context_packet

    parsed = MagicMock()
    parsed.llm_context_packet = {
        "case_id": "spi-demo",
        "variant": "buggy",
        "source_rel": "drivers/spi/demo.c",
        "module_path": "drivers/spi",
        "kbuild_target": "demo.o",
        "entrypoints": [
            {"name": "demo_probe", "kind": "callback:probe", "line": 10, "evidence": ".probe = demo_probe"},
            {"name": "demo_remove", "kind": "callback:remove", "line": 20, "evidence": ".remove = demo_remove"},
        ],
        "candidate_properties": [
            {
                "id": "contract:spi-controller-devm-teardown-order",
                "property": "dependent resource teardown before unregister of devm-managed spi controller",
                "confidence": "high",
            }
        ],
        "kernel_api_contracts": [
            {
                "id": "spi-controller-devm-teardown-order",
                "status": "candidate_triggered",
                "matched_apis": ["devm_spi_register_controller", "pm_runtime_disable"],
                "contract": "devres unregisters the SPI controller after driver remove returns",
            }
        ],
        "focused_source_snippets": [],
    }

    context = _format_driver_context_packet(
        parsed,
        func_name="bmc_agent_lifecycle_demo_probe_then_demo_remove",
        failing_property="bmc_agent_spi_devm_dependency_teardown.assertion.1",
    )

    assert "Linux framework callbacks" in context
    assert "bmc_agent_lifecycle_* function models a kernel device lifecycle" in context
    assert "bmc_agent_* functions are synthetic kernel-contract wrappers" in context
    assert "probe_ret == 0" in context


def test_driver_entrypoint_annotation_overrides_address_taken_callback_guard() -> None:
    from bmc_agent.cex_validator import _is_kernel_driver_entrypoint

    parsed = SimpleNamespace(
        bmc_agent_driver_entrypoints=[
            {"name": "adis_init", "kind": "public_function"},
            {"name": "demo_probe", "kind": "callback:probe"},
        ]
    )
    plain = SimpleNamespace(bmc_agent_driver_entrypoints=[])

    assert _is_kernel_driver_entrypoint("adis_init", parsed)
    assert _is_kernel_driver_entrypoint("demo_probe", parsed)
    assert not _is_kernel_driver_entrypoint("qsort_compare", plain)


def test_kernel_driver_mode_generates_usb_lifecycle_harness() -> None:
    import importlib.util
    import sys

    script = (
        REPO_ROOT
        / "experiments"
        / "linux_driver_kernel_mode"
        / "kernel_driver_mode.py"
    )
    spec = importlib.util.spec_from_file_location("kernel_driver_mode_usb_test_mod", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    discovery = mod.Discovery(
        package="pkg",
        source_i="source.i",
        analysis_source="analysis.c",
        driver_functions=["demo_probe", "demo_disconnect"],
        entrypoints=[
            mod.EntryPoint("demo_probe", "callback:probe", "probe table", 100),
            mod.EntryPoint("demo_disconnect", "callback:disconnect", "disconnect table", 100),
        ],
        driver_closure=["demo_probe", "demo_disconnect"],
        external_symbols=[],
    )
    parsed = SimpleNamespace(
        functions={
            "demo_probe": SimpleNamespace(
                parameters=[
                    ("struct usb_interface *", "intf"),
                    ("const struct usb_device_id *", "id"),
                ],
                return_type="int",
            ),
            "demo_disconnect": SimpleNamespace(
                parameters=[("struct usb_interface *", "intf")],
                return_type="void",
            ),
        }
    )

    harnesses = mod.generated_lifecycle_harnesses(discovery, parsed)

    harness = next(
        h for h in harnesses if h.kind == "usb_probe_success_then_disconnect"
    )
    assert harness.kind == "usb_probe_success_then_disconnect"
    assert harness.entrypoints == ["demo_probe", "demo_disconnect"]
    assert "struct usb_interface __bmc_usbif" in harness.source
    assert "__bmc_usbdev.serial = (char *)0" in harness.source
    assert "demo_probe(&__bmc_usbif, &__bmc_id)" in harness.source
    assert "demo_disconnect(&__bmc_usbif)" in harness.source


def test_kernel_driver_api_spend_gate_go_and_hold() -> None:
    import importlib.util
    import sys

    script = (
        REPO_ROOT
        / "experiments"
        / "linux_driver_kernel_mode"
        / "kernel_driver_mode.py"
    )
    spec = importlib.util.spec_from_file_location("kernel_driver_mode_gate_test_mod", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    scan_report = {
        "status": "ready",
        "api_marker_scan": {"marker_count": 0},
        "batch": {
            "timed_out": 0,
            "cbmc": {
                "errors": 0,
                "non_no_body_counterexamples": 1,
            },
        },
    }
    selected = {
        "case": "spi-demo",
        "variant": "buggy",
        "failing_property": "bmc_agent_spi_devm_dependency_teardown.assertion.1",
        "reason": "mode:paired,assertion-property,framework-contract,lifecycle-root,buggy-only-vs-fixed",
        "present_in_fixed": False,
    }
    triage = {
        "candidate_selection_mode": "paired",
        "selected_candidates": [selected],
    }
    plan = {
        "groups": [
            {
                "case": "spi-demo",
                "variant": "buggy",
                "properties": [selected["failing_property"]],
            }
        ]
    }

    gate = mod.evaluate_api_spend_gate(
        scan_report=scan_report,
        triage=triage,
        plan=plan,
        allow_targeted_llm=False,
    )

    assert gate["decision"] == "go_for_targeted_realism"
    assert gate["eligible_for_targeted_llm"] is True
    assert gate["estimated_llm_calls"] == 2
    assert "classification + realism" in gate["estimated_llm_calls_basis"]
    assert gate["blockers"] == []

    hold_gate = mod.evaluate_api_spend_gate(
        scan_report=scan_report,
        triage={"candidate_selection_mode": "paired", "selected_candidates": []},
        plan={"groups": []},
        allow_targeted_llm=False,
    )

    assert hold_gate["decision"] == "hold"
    assert hold_gate["eligible_for_targeted_llm"] is False
    assert "no selected candidate from cheap triage" in hold_gate["blockers"]
