"""
Phase 2: Compositional BMC Engine [CONVENTIONAL].

Deterministic tool invocation — not agentic. Exposes one interface:
  check(function, spec, callee_specs) -> {verified, counterexample}
The agentic layers use this interface; swapping CBMC for another backend
changes only the harness-synthesis-and-invocation code here.
"""

from __future__ import annotations

import dataclasses
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bmc_agent.artifacts import ArtifactStore
from bmc_agent.backends import BMCBackend, CBMCBackend
from bmc_agent.cbmc import CBMCResult, Counterexample, run_cbmc
from bmc_agent.config import Config
from bmc_agent.harness_generator import HarnessGenerator
from bmc_agent.logger import get_logger
from bmc_agent.parser import FunctionInfo, ParsedCFile
from bmc_agent.spec import Spec

logger = get_logger("bmc_engine")


@dataclass
class BMCVerdict:
    """Result of running BMC on a single function."""

    function_name: str
    verified: bool                          # True = verified up to bound k
    counterexamples: list[Counterexample] = field(default_factory=list)
    harness_path: str = ""
    cbmc_result: Optional[CBMCResult] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        # CBMCResult and Counterexample are already handled by asdict
        return d


class BMCEngine:
    """Runs CBMC on function harnesses to check specs."""

    def __init__(
        self,
        config: Config,
        store: ArtifactStore,
        backend: "BMCBackend | None" = None,
    ) -> None:
        self.config = config
        self.store = store
        self.harness_gen = HarnessGenerator(config)  # kept for backward compat
        self.backend: BMCBackend = backend or CBMCBackend(config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_function(
        self,
        func: FunctionInfo,
        spec: Spec,
        parsed_file: ParsedCFile,
        driver_name: str,
        all_funcs: "dict | None" = None,
        flag_selection: "object | None" = None,
    ) -> BMCVerdict:
        """
        Check a single function against its spec using CBMC.

        Steps:
        1. Generate a CBMC harness.
        2. Save harness to the artifact directory.
        3. Run CBMC.
        4. Return a structured BMCVerdict.
        """
        fn_name = func.name
        logger.info("Checking function '%s' (driver '%s')", fn_name, driver_name)

        # ---- Step 1: generate harness ----
        try:
            harness_src = self.backend.generate_harness(func, spec, {}, parsed_file, all_funcs=all_funcs)
        except Exception as exc:
            logger.error("Harness generation failed for '%s': %s", fn_name, exc)
            return BMCVerdict(
                function_name=fn_name,
                verified=False,
                error=f"Harness generation failed: {exc}",
            )

        # ---- Step 2: save harness ----
        harness_path = self._save_harness(driver_name, fn_name, harness_src)
        logger.debug("Harness saved to: %s", harness_path)

        # ---- Step 3: run CBMC ----
        # Baseline flags come from the threat model; per-function flag selection
        # adds on top (OR-merged so either source can enable a check).
        threat_model = getattr(self.config, "threat_model", "security")
        pointer_check    = threat_model in ("security", "safety")
        bounds_check     = threat_model in ("security", "safety")
        div_by_zero_check = threat_model == "safety"

        unsigned_overflow_check = bool(getattr(flag_selection, "unsigned_overflow_check", False))
        signed_overflow_check   = bool(getattr(flag_selection, "signed_overflow_check", False))
        conversion_check        = bool(getattr(flag_selection, "conversion_check", False))
        pointer_overflow_check  = bool(getattr(flag_selection, "pointer_overflow_check", False))

        if flag_selection and flag_selection.any_enabled():
            logger.debug(
                "Flag selection for '%s': %s (%s)",
                fn_name,
                ", ".join(flag_selection.enabled_flags()),
                getattr(flag_selection, "reasoning", ""),
            )
        cbmc_result = run_cbmc(
            harness_path=harness_path,
            unwind=self.config.cbmc_unwind,
            timeout=self.config.cbmc_timeout,
            cbmc_path=self.config.cbmc_path,
            include_dirs=getattr(self.config, "include_dirs", None),
            unsigned_overflow_check=unsigned_overflow_check,
            signed_overflow_check=signed_overflow_check,
            conversion_check=conversion_check,
            pointer_overflow_check=pointer_overflow_check,
            pointer_check=pointer_check,
            bounds_check=bounds_check,
            div_by_zero_check=div_by_zero_check,
        )

        # ---- Step 4: build verdict ----
        if cbmc_result.error:
            logger.warning(
                "CBMC error for '%s': %s", fn_name, cbmc_result.error
            )
            verdict = BMCVerdict(
                function_name=fn_name,
                verified=False,
                counterexamples=cbmc_result.counterexamples,
                harness_path=str(harness_path),
                cbmc_result=cbmc_result,
                error=cbmc_result.error,
            )
        else:
            logger.info(
                "CBMC verdict for '%s': verified=%s, counterexamples=%d",
                fn_name,
                cbmc_result.verified,
                len(cbmc_result.counterexamples),
            )
            verdict = BMCVerdict(
                function_name=fn_name,
                verified=cbmc_result.verified,
                counterexamples=cbmc_result.counterexamples,
                harness_path=str(harness_path),
                cbmc_result=cbmc_result,
                error=None,
            )

        # ---- Save results to artifact store ----
        try:
            self.store.save_cbmc_result(driver_name, fn_name, cbmc_result)
            self.store.save_bug_report(driver_name, fn_name, verdict.to_dict())
        except Exception as exc:
            logger.warning("Failed to save artifacts for '%s': %s", fn_name, exc)

        return verdict

    def check_all(
        self,
        funcs: dict[str, FunctionInfo],
        specs: dict[str, Spec],
        parsed_file: ParsedCFile,
        driver_name: str,
        all_funcs: "dict | None" = None,
        flag_selections: "dict | None" = None,
    ) -> dict[str, BMCVerdict]:
        """
        Check all functions in parallel (ThreadPoolExecutor).

        Parameters
        ----------
        funcs:
            Mapping function_name → FunctionInfo.
        specs:
            Mapping function_name → Spec.
        parsed_file:
            The parsed C file object.
        driver_name:
            Driver name for artifact storage.

        Returns
        -------
        Mapping function_name → BMCVerdict.
        """
        verdicts: dict[str, BMCVerdict] = {}

        # Only check functions that have both a FunctionInfo and a Spec
        to_check = {
            name: funcs[name]
            for name in funcs
            if name in specs
        }
        if not to_check:
            logger.warning("No functions to check in driver '%s'", driver_name)
            return verdicts

        max_workers = min(len(to_check), self.config.batch_size, 8)
        logger.info(
            "Checking %d functions in driver '%s' with %d workers",
            len(to_check),
            driver_name,
            max_workers,
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_name = {
                executor.submit(
                    self.check_function,
                    func,
                    specs[name],
                    parsed_file,
                    driver_name,
                    all_funcs,
                    (flag_selections or {}).get(name),
                ): name
                for name, func in to_check.items()
            }
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    verdict = future.result()
                    verdicts[name] = verdict
                except Exception as exc:
                    logger.error(
                        "Unexpected error checking '%s': %s", name, exc
                    )
                    verdicts[name] = BMCVerdict(
                        function_name=name,
                        verified=False,
                        error=f"Unexpected error: {exc}",
                    )

        return verdicts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save_harness(
        self,
        driver_name: str,
        func_name: str,
        harness_src: str,
    ) -> Path:
        """
        Save the harness source to
        ``{artifact_dir}/{driver_name}/{func_name}/harness.c``.
        """
        fn_dir = (
            Path(self.config.artifact_dir) / driver_name / func_name
        )
        fn_dir.mkdir(parents=True, exist_ok=True)
        harness_path = fn_dir / "harness.c"
        harness_path.write_text(harness_src, encoding="utf-8")
        return harness_path
