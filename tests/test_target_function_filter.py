from bmc_agent.cli import build_parser
from bmc_agent.spec_generator import _target_with_local_callee_closure as closure_v1
from bmc_agent.spec_generator_v2 import _target_with_local_callee_closure as closure_v2


def test_target_function_filter_keeps_transitive_in_file_callees_only():
    call_graph = {
        "entry": {"helper", "external_api"},
        "helper": {"leaf"},
        "leaf": set(),
        "unrelated": {"leaf"},
    }
    defined = {"entry", "helper", "leaf", "unrelated"}

    expected = {"entry", "helper", "leaf"}
    assert closure_v1({"entry"}, call_graph, defined) == expected
    assert closure_v2({"entry"}, call_graph, defined) == expected


def test_target_function_filter_missing_target_is_empty():
    assert closure_v1({"missing"}, {"entry": set()}, {"entry"}) == set()
    assert closure_v2({"missing"}, {"entry": set()}, {"entry"}) == set()


def test_verify_cli_accepts_function_filters():
    args = build_parser().parse_args(
        [
            "verify",
            "--source",
            "driver.c",
            "--driver",
            "driver",
            "--function",
            "entry",
            "--functions",
            "helper,leaf",
        ]
    )

    assert args.function == "entry"
    assert args.functions == "helper,leaf"
