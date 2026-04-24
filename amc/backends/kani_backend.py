"""Kani backend stub for Rust programs. Not yet implemented."""
from __future__ import annotations
from amc.backends.bmc_backend import BMCBackend


class KaniBackend(BMCBackend):
    """Stub backend for Kani (Rust bounded model checker). Not yet implemented."""

    @property
    def language(self) -> str:
        return "rust"

    def generate_harness(self, func, spec, callee_specs, parsed_file) -> str:
        raise NotImplementedError(
            "Kani harness generation is not yet implemented. "
            "Rust/Kani support is planned for a future release."
        )

    def check(self, harness_path) -> object:
        raise NotImplementedError(
            "Kani backend checking is not yet implemented."
        )
