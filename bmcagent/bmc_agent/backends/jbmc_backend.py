"""JBMC backend for Java whole-program checks."""

from __future__ import annotations

from bmc_agent.backends.bmc_backend import BMCBackend


class JBMCBackend(BMCBackend):
    """JBMC backend for Java programs.

    Java support currently verifies compiled Java bytecode directly with JBMC.
    It does not synthesize per-function C-style harnesses.
    """

    def __init__(self, config) -> None:
        self._config = config

    @property
    def language(self) -> str:
        return "java"

    def generate_harness(self, func, spec, callee_specs, parsed_file, all_funcs=None) -> str:
        raise NotImplementedError(
            "Java/JBMC support verifies Java sources directly; per-function "
            "BMC-Agent harness synthesis is not implemented for Java."
        )

    def check(self, harness_path) -> object:
        from bmc_agent.jbmc import run_jbmc

        return run_jbmc(
            source_path=harness_path,
            entry="main",
            unwind=getattr(self._config, "jbmc_unwind", getattr(self._config, "cbmc_unwind", 4)),
            timeout=getattr(self._config, "jbmc_timeout", getattr(self._config, "cbmc_timeout", 120)),
            jbmc_path=getattr(self._config, "jbmc_path", "jbmc"),
            javac_path=getattr(self._config, "javac_path", "javac"),
            classpath=list(getattr(self._config, "java_classpath", []) or []),
            compile_timeout=getattr(self._config, "java_compile_timeout", 60),
        )
