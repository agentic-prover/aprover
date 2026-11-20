"""Pin the canonical agent-role registry against its historical 11-role set.

``bmc_agent.agent_registry`` is the single source of truth that ``config.py``
(env routing) and ``cli.py`` (``ALL_AGENT_ROLES``) now derive from. This test
fails loudly if a role is silently added/removed/renamed, which is exactly the
drift class that let ``AgenticHarnessGen`` borrow ``role=\"realism\"`` instead of
owning its own routed role.
"""

from bmc_agent.agent_registry import AGENT_ROLES, REGISTRY, label_for

# The exact historical set (order-insensitive). Edit this ONLY alongside a
# deliberate role add/retire.
HISTORICAL_ROLES = {
    "spec_gen",
    "feedback_distill",
    "refinement",
    "realism",
    "classifier",
    "disagreement_diagnose",
    "triage",
    "dynamic_repro",
    "dynval_triage",
    "cbmc_driver",
    "harness_gen",
}


def test_agent_roles_match_historical_set():
    assert set(AGENT_ROLES) == HISTORICAL_ROLES


def test_agent_roles_count_is_eleven():
    assert len(AGENT_ROLES) == 11


def test_agent_roles_have_no_duplicates():
    assert len(AGENT_ROLES) == len(set(AGENT_ROLES))


def test_registry_roles_align_with_agent_roles():
    assert tuple(spec.role for spec in REGISTRY) == AGENT_ROLES


def test_label_for_known_and_unknown():
    # known role -> its display label
    assert label_for("dynamic_repro") == "reproducer"
    # unknown role -> falls back to the role string itself
    assert label_for("does_not_exist") == "does_not_exist"


def test_config_consumes_registry():
    # config.py derives its env-routing loop from AGENT_ROLES.
    import bmc_agent.config as config

    assert config.AGENT_ROLES is AGENT_ROLES


def test_cli_consumes_registry():
    # cli.py exposes AGENT_ROLES (imported from the registry) as the source for
    # ALL_AGENT_ROLES inside _apply_provider_args.
    import bmc_agent.cli as cli

    assert cli.AGENT_ROLES is AGENT_ROLES
