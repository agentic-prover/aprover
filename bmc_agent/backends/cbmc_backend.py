"""CBMC backend: delegates to existing HarnessGenerator and run_cbmc."""
from __future__ import annotations
from pathlib import Path
from bmc_agent.backends.bmc_backend import BMCBackend


class CBMCBackend(BMCBackend):
    """CBMC backend for C programs."""

    def __init__(self, config) -> None:
        from bmc_agent.harness_generator import HarnessGenerator
        self._config = config
        self._harness_gen = HarnessGenerator(config)

    @property
    def language(self) -> str:
        return "c"

    def generate_harness(self, func, spec, callee_specs, parsed_file, all_funcs=None) -> str:
        return self._harness_gen.generate_harness(func, spec, parsed_file, all_funcs=all_funcs)

    def check(self, harness_path) -> object:
        from bmc_agent.cbmc import run_cbmc
        return run_cbmc(
            harness_path=str(harness_path),
            unwind=self._config.cbmc_unwind,
            timeout=self._config.cbmc_timeout,
            cbmc_path=self._config.cbmc_path,
            include_dirs=getattr(self._config, "include_dirs", None),
        )
