"""
Baseline runners for BMC-Agent evaluation.

Provides comparison baselines:
- CBMCAloneBaseline: runs CBMC directly without LLM-generated specs.
- AMCAblationBaseline: BMC-Agent with bottom-up spec generation instead of top-down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bmc_agent.cbmc import run_cbmc

if TYPE_CHECKING:
    from bmc_agent.artifacts import ArtifactStore
    from bmc_agent.config import Config


@dataclass
class BaselineResult:
    """Result of running a baseline tool on a single driver."""

    name: str                   # "cbmc_alone", "smatch", "amc_ablation"
    driver_name: str
    bugs_found: list[str] = field(default_factory=list)   # list of bug descriptions
    false_positives: int = 0
    runtime_seconds: float = 0.0
    error: str | None = None


class CBMCAloneBaseline:
    """
    Run CBMC directly on the source without LLM-generated specs.

    Only uses CBMC's built-in safety properties (null deref, overflow, OOB).
    No callee stubs, no spec assertions — unconstrained nondeterministic inputs.
    """

    def run(
        self,
        source_file: str,
        driver_name: str,
        config: "Config",
        store: "ArtifactStore",
    ) -> BaselineResult:
        """
        For each function in the source file, generate a minimal harness
        (unconstrained nondeterministic inputs, no callee stubs, no spec
        assertions) and run CBMC.

        Only catches safety violations CBMC detects natively:
        - Null pointer dereferences
        - Integer overflows
        - Array out-of-bounds
        """
        from bmc_agent.parser import parse_c_file

        start = time.monotonic()
        bugs_found: list[str] = []
        errors: list[str] = []

        try:
            parsed = parse_c_file(source_file)
        except Exception as exc:
            return BaselineResult(
                name="cbmc_alone",
                driver_name=driver_name,
                runtime_seconds=time.monotonic() - start,
                error=f"Parse error: {exc}",
            )

        for fn_name, func_sig in parsed.functions.items():
            harness_src = _make_minimal_harness(
                source_file=source_file,
                func_sig=func_sig,
            )

            import tempfile

            with tempfile.NamedTemporaryFile(
                suffix=".c", delete=False, mode="w", encoding="utf-8"
            ) as tmp:
                tmp.write(harness_src)
                tmp_path = tmp.name

            try:
                result = run_cbmc(
                    harness_path=tmp_path,
                    unwind=config.cbmc_unwind,
                    timeout=config.cbmc_timeout,
                    cbmc_path=config.cbmc_path,
                )
            finally:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

            if result.error:
                errors.append(f"{fn_name}: {result.error}")
                continue

            for cex in result.counterexamples:
                bugs_found.append(
                    f"{fn_name}: {cex.failing_property}"
                )

        runtime = time.monotonic() - start
        error_str = "; ".join(errors) if errors else None

        return BaselineResult(
            name="cbmc_alone",
            driver_name=driver_name,
            bugs_found=bugs_found,
            false_positives=0,    # not classified without LLM validation
            runtime_seconds=runtime,
            error=error_str,
        )


class AMCAblationBaseline:
    """
    BMC-Agent with bottom-up spec generation instead of top-down.

    Generates specs from the implementation alone (no caller context),
    then runs the same BMC + validation pipeline.
    """

    def run(
        self,
        source_file: str,
        driver_name: str,
        config: "Config",
    ) -> BaselineResult:
        """
        Run BMC-Agent but generate specs without using caller context.

        This ablation removes the top-down refinement aspect of BMC-Agent,
        serving as a baseline to measure the value of caller-context-aware
        spec generation.
        """
        from bmc_agent.artifacts import ArtifactStore
        from bmc_agent.llm import LLMClient
        from bmc_agent.parser import parse_c_file
        from bmc_agent.spec_generator import SpecGenerator

        start = time.monotonic()
        bugs_found: list[str] = []

        # Use a distinct artifact subdirectory
        ablation_config = _copy_config_with_suffix(config, "_ablation")
        store = ArtifactStore(ablation_config.artifact_dir)
        llm = LLMClient(ablation_config)

        try:
            spec_gen = SpecGenerator(ablation_config, llm, store)
            # Generate specs with empty domain knowledge (no caller context)
            specs = spec_gen.generate_specs(
                source_file=source_file,
                driver_name=driver_name,
                domain_knowledge="",  # ablation: no caller context
            )
        except Exception as exc:
            return BaselineResult(
                name="amc_ablation",
                driver_name=driver_name,
                runtime_seconds=time.monotonic() - start,
                error=f"Spec generation failed: {exc}",
            )

        # Run BMC
        from bmc_agent.bmc_engine import BMCEngine

        try:
            parsed = parse_c_file(source_file)
            engine = BMCEngine(ablation_config, store)
            funcs = {
                name: parsed.get_function_info(name)
                for name in specs
                if parsed.get_function_info(name) is not None
            }
            verdicts = engine.check_all(funcs, specs, parsed, driver_name)
        except Exception as exc:
            return BaselineResult(
                name="amc_ablation",
                driver_name=driver_name,
                runtime_seconds=time.monotonic() - start,
                error=f"BMC failed: {exc}",
            )

        for fn_name, verdict in verdicts.items():
            if not verdict.verified and verdict.counterexamples:
                for cex in verdict.counterexamples:
                    bugs_found.append(f"{fn_name}: {cex.failing_property}")

        return BaselineResult(
            name="amc_ablation",
            driver_name=driver_name,
            bugs_found=bugs_found,
            false_positives=0,
            runtime_seconds=time.monotonic() - start,
        )


class FilteringOnlyBaseline:
    """
    AMC with filtering but no refinement (V3 ablation baseline for RQ3).

    Runs the full AMC pipeline with skip_refinement=True: counterexamples are
    classified as REAL/SPURIOUS/UNRESOLVED and confirmed-spurious ones are
    filtered, but the spec is never updated and callers are never re-queued.

    Measures whether refinement's complexity is justified over simple filtering.
    """

    def run(
        self,
        source_file: str,
        driver_name: str,
        config: "Config",
    ) -> BaselineResult:
        from dataclasses import replace

        from bmc_agent.pipeline import AMCPipeline

        start = time.monotonic()

        filtering_config = replace(config, skip_refinement=True)  # type: ignore[call-arg]

        try:
            pipeline = AMCPipeline(filtering_config)
            bug_reports = pipeline.run(
                source_file=source_file,
                driver_name=driver_name + "_filtering_only",
            )
        except Exception as exc:
            return BaselineResult(
                name="filtering_only",
                driver_name=driver_name,
                runtime_seconds=time.monotonic() - start,
                error=f"Pipeline failed: {exc}",
            )

        bugs_found = [
            f"{r.function_name}: {r.bug_type}" for r in bug_reports
        ]

        return BaselineResult(
            name="filtering_only",
            driver_name=driver_name,
            bugs_found=bugs_found,
            false_positives=0,
            runtime_seconds=time.monotonic() - start,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_harness(source_file: str, func_sig: "object") -> str:
    """
    Generate a minimal CBMC harness for a function.

    Uses __CPROVER_nondet_* for all inputs; no callee stubs; no spec assertions.
    Only checks built-in CBMC safety properties.

    Parameters
    ----------
    source_file:
        Path to the C source file (used in the #include).
    func_sig:
        A FunctionSignature object with .name, .return_type, and .parameters.
    """
    from bmc_agent.parser import FunctionSignature

    sig: FunctionSignature = func_sig  # type: ignore[assignment]

    # Use absolute path so the harness compiles correctly from any working directory.
    abs_source = str(Path(source_file).resolve())

    lines: list[str] = [
        f'#include "{abs_source}"',
        "",
        "/* CBMC-alone minimal harness (no spec assertions) */",
        "void __CPROVER_assume(_Bool cond);",
        "",
        "int main(void) {",
    ]

    param_vars: list[str] = []
    for idx, (ptype, pname) in enumerate(sig.parameters):
        var = pname if pname else f"arg{idx}"
        ctype = ptype.strip().rstrip("*").strip()
        if "*" in ptype:
            # All pointer params: unconstrained (NULL or any address).
            # CBMC explores both the NULL path (triggering NULL checks) and
            # nondeterministic non-NULL paths.
            lines.append(f"    {ptype} {var};")
        elif ctype in ("int", "unsigned int", "uint32_t", "int32_t"):
            lines.append(f"    {ptype} {var};")
        elif ctype in ("size_t", "uint64_t", "unsigned long"):
            lines.append(f"    {ptype} {var};")
        else:
            lines.append(f"    {ptype} {var};")
        param_vars.append(var)

    args = ", ".join(param_vars)
    ret_type = sig.return_type.strip()
    if ret_type and ret_type != "void":
        lines.append(f"    {ret_type} ret = {sig.name}({args});")
    else:
        lines.append(f"    {sig.name}({args});")

    lines.append("    return 0;")
    lines.append("}")

    return "\n".join(lines) + "\n"


def _copy_config_with_suffix(config: "Config", suffix: str) -> "Config":
    """Return a shallow copy of config with a modified artifact_dir."""
    from dataclasses import replace

    return replace(config, artifact_dir=config.artifact_dir + suffix)  # type: ignore[call-arg]
