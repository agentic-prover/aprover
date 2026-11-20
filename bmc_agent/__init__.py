"""
AMC: Agentic Model Checking

LLM agents generate formal specs for C functions, CBMC compositionally
verifies each function against its spec, and an agentic counterexample
confirmation pipeline classifies verdicts as real bugs, spurious, or unresolved.
"""

__version__ = "0.1.0"
__all__ = [
    "config",
    "llm",
    "cbmc",
    "parser",
    "spec",
    "artifacts",
    "logger",
]
