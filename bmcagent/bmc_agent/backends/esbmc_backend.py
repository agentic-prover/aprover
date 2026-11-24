"""ESBMC C backend using the existing deterministic harness generator."""
from bmc_agent.backends.cbmc_backend import CBMCBackend
from bmc_agent.esbmc import run_esbmc


class ESBMCBackend(CBMCBackend):
    def check(self, harness_path):
        return run_esbmc(
            harness_path, esbmc_path=self._config.esbmc_path,
            unwind=self._config.cbmc_unwind, timeout=self._config.cbmc_timeout,
            include_dirs=getattr(self._config, "include_dirs", None),
            defines=getattr(self._config, "cbmc_defines", None),
            preserve_ldv_property=getattr(self._config, "preserve_ldv_property", False),
        )
