"""Abstract base class for BMC backends."""
from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path


class BMCBackend(ABC):
    """Abstract interface for bounded model checking backends."""

    @abstractmethod
    def generate_harness(
        self,
        func,           # FunctionInfo
        spec,           # Spec
        callee_specs: dict,
        parsed_file,    # ParsedCFile
    ) -> str:
        """Generate harness source code. Returns C (or Rust) source as string."""
        ...

    @abstractmethod
    def check(self, harness_path: "str | Path") -> object:
        """Run backend on harness_path. Returns a CBMCResult-like object."""
        ...

    @property
    @abstractmethod
    def language(self) -> str:
        """Return 'c' or 'rust'."""
        ...
