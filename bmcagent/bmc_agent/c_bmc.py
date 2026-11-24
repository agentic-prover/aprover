"""Single dispatch gate for C verification, validation and refinement queries.

The explicitly named CBMC-alone evaluation baseline deliberately bypasses this
gate. Result dataclasses and historical cbmc_* configuration names stay stable.
"""
from __future__ import annotations

from bmc_agent.cbmc import run_cbmc


def backend_name(config) -> str:
    name = getattr(config, "c_backend", "cbmc").lower()
    if name not in ("cbmc", "esbmc"):
        raise ValueError(f"Unsupported C backend: {name}")
    return name


def executable_for(config) -> str:
    name = backend_name(config)
    return getattr(config, name + "_path", name)


def run_c_bmc(*args, config, **kwargs):
    kwargs.setdefault("unwind", getattr(config, "cbmc_unwind", 4))
    kwargs.setdefault("timeout", getattr(config, "cbmc_timeout", 120))
    if backend_name(config) == "cbmc":
        kwargs.setdefault("cbmc_path", executable_for(config))
        return run_cbmc(*args, **kwargs)
    from bmc_agent.esbmc import run_esbmc
    kwargs.pop("cbmc_path", None)
    kwargs.setdefault("esbmc_path", executable_for(config))
    kwargs.setdefault("preserve_ldv_property", getattr(config, "preserve_ldv_property", False))
    return run_esbmc(*args, **kwargs)
