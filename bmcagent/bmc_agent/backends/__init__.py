from bmc_agent.backends.bmc_backend import BMCBackend
from bmc_agent.backends.cbmc_backend import CBMCBackend
from bmc_agent.backends.esbmc_backend import ESBMCBackend
from bmc_agent.backends.jbmc_backend import JBMCBackend
from bmc_agent.backends.kani_backend import KaniBackend


def backend_for(language: str, config) -> BMCBackend:
    """Return the BMC backend appropriate for *language*.

    ``language`` is one of the values produced by
    :func:`bmc_agent.source_parser.detect_language` — ``"c"`` for C/header
    files, ``"rust"`` for ``.rs`` files, and ``"java"`` for ``.java`` files.
    C and unknown languages use ``config.c_backend`` (CBMC by default).
    """
    if language == "rust":
        return KaniBackend(config)
    if language == "java":
        return JBMCBackend(config)
    from bmc_agent.c_bmc import backend_name
    return ESBMCBackend(config) if backend_name(config) == "esbmc" else CBMCBackend(config)


__all__ = ["BMCBackend", "CBMCBackend", "ESBMCBackend", "JBMCBackend", "KaniBackend", "backend_for"]
