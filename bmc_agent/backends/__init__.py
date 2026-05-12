from bmc_agent.backends.bmc_backend import BMCBackend
from bmc_agent.backends.cbmc_backend import CBMCBackend
from bmc_agent.backends.kani_backend import KaniBackend


def backend_for(language: str, config) -> BMCBackend:
    """Return the BMC backend appropriate for *language*.

    ``language`` is one of the values produced by
    :func:`bmc_agent.source_parser.detect_language` — ``"c"`` for C/header
    files, ``"rust"`` for ``.rs`` files.  Unknown languages fall back to
    :class:`CBMCBackend` to preserve existing behaviour for callers that
    don't pre-detect.
    """
    if language == "rust":
        return KaniBackend(config)
    return CBMCBackend(config)


__all__ = ["BMCBackend", "CBMCBackend", "KaniBackend", "backend_for"]
