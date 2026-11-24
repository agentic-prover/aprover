"""
Artifact directory layout for BMC-Agent.

One subdirectory per driver; one sub-subdirectory per function.
Each result is saved as a JSON file inside the function directory.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bmc_agent.spec import Spec, SpecStatus


class ArtifactStore:
    """
    Manages the on-disk artifact layout for a BMC-Agent verification run.

    Layout::

        {base_dir}/
            amc.log
            {driver}/
                {function}/
                    spec.json
                    cbmc_result.json
                    bug_report.json
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Driver management
    # ------------------------------------------------------------------

    def init_driver(self, driver_name: str) -> Path:
        """Create and return the directory for a driver."""
        driver_dir = self.base_dir / driver_name
        driver_dir.mkdir(parents=True, exist_ok=True)
        return driver_dir

    def _fn_dir(self, driver: str, function: str) -> Path:
        """Return (and create) the directory for a specific function."""
        d = self.base_dir / driver / function
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------
    # Spec storage
    # ------------------------------------------------------------------

    def save_spec(self, driver: str, function: str, spec: Spec) -> Path:
        """Serialise and save a Spec to ``{driver}/{function}/spec.json``."""
        path = self._fn_dir(driver, function) / "spec.json"
        payload = {
            "saved_at": _utcnow(),
            "spec": spec.to_dict(),
        }
        _write_json(path, payload)
        return path

    def load_spec(self, driver: str, function: str) -> Optional[Spec]:
        """Load a Spec from disk, or return None if it does not exist."""
        path = self._fn_dir(driver, function) / "spec.json"
        if not path.exists():
            return None
        data = _read_json(path)
        return Spec.from_dict(data["spec"])

    # ------------------------------------------------------------------
    # CBMC result storage
    # ------------------------------------------------------------------

    def save_cbmc_result(self, driver: str, function: str, result: Any) -> Path:
        """Save a CBMCResult (or any JSON-serialisable object) to disk."""
        path = self._fn_dir(driver, function) / "cbmc_result.json"
        payload: dict[str, Any] = {
            "saved_at": _utcnow(),
        }
        # CBMCResult is a dataclass; handle both dataclasses and plain dicts.
        if hasattr(result, "__dataclass_fields__"):
            import dataclasses

            payload["result"] = dataclasses.asdict(result)
        elif isinstance(result, dict):
            payload["result"] = result
        else:
            payload["result"] = str(result)
        _write_json(path, payload)
        return path

    def load_cbmc_result(self, driver: str, function: str) -> Optional[dict]:
        """Load a CBMC result dict from disk."""
        path = self._fn_dir(driver, function) / "cbmc_result.json"
        if not path.exists():
            return None
        return _read_json(path).get("result")

    # ------------------------------------------------------------------
    # Bug report storage
    # ------------------------------------------------------------------

    def save_bug_report(self, driver: str, function: str, report: Any) -> Path:
        """Save a bug report. Each (function, failing_property) pair gets its
        own file so multi-CEx functions don't overwrite earlier verdicts.

        Layout:
          ``{driver}/{function}/bug_report.json``                   — latest CEx (back-compat)
          ``{driver}/{function}/bug_reports/<property_safe>.json``  — per-CEx history (preserved)
        """
        fn_dir = self._fn_dir(driver, function)
        path = fn_dir / "bug_report.json"
        payload: dict[str, Any] = {"saved_at": _utcnow()}
        if isinstance(report, dict):
            payload["report"] = report
        elif hasattr(report, "__dataclass_fields__"):
            import dataclasses
            payload["report"] = dataclasses.asdict(report)
        else:
            payload["report"] = str(report)
        # Latest-CEx file (kept for back-compat with readers that look here).
        _write_json(path, payload)
        # Per-CEx historical record. Derive a filesystem-safe property name
        # from the report's failing_property (or the counterexample's). When
        # no property name is available, fall back to a timestamped suffix so
        # we still preserve each save.
        try:
            r = payload.get("report") or {}
            prop = ""
            if isinstance(r, dict):
                prop = (
                    r.get("violated_property")
                    or ((r.get("counterexample") or {}).get("failing_property") if isinstance(r.get("counterexample"), dict) else "")
                    or ""
                )
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(prop))[:120] \
                   or f"unnamed_{int(time.time()*1000)}"
            (fn_dir / "bug_reports").mkdir(parents=True, exist_ok=True)
            _write_json(fn_dir / "bug_reports" / f"{safe}.json", payload)
        except Exception:
            # History recording is best-effort; do not fail the save if it
            # can't be written (e.g. read-only FS in tests).
            pass
        return path

    def load_bug_report(self, driver: str, function: str) -> Optional[dict]:
        """Load a bug report dict from disk."""
        path = self._fn_dir(driver, function) / "bug_report.json"
        if not path.exists():
            return None
        return _read_json(path).get("report")

    def save_latent_report(self, driver: str, function: str, report: Any) -> Path:
        """Save a LATENT bug report to ``{driver}/{function}/latent_report.json``.

        Latent reports are panics reachable via the public API but not
        from any in-tree caller — cargo-fuzz / future-caller risk. They
        live in a separate file from ``bug_report.json`` so triage can
        pick severity tier (reachable vs latent) without parsing.
        """
        path = self._fn_dir(driver, function) / "latent_report.json"
        payload: dict[str, Any] = {"saved_at": _utcnow()}
        if isinstance(report, dict):
            payload["report"] = report
        elif hasattr(report, "__dataclass_fields__"):
            import dataclasses
            payload["report"] = dataclasses.asdict(report)
        else:
            payload["report"] = str(report)
        _write_json(path, payload)
        return path

    def load_latent_report(self, driver: str, function: str) -> Optional[dict]:
        path = self._fn_dir(driver, function) / "latent_report.json"
        if not path.exists():
            return None
        return _read_json(path).get("report")

    # ------------------------------------------------------------------
    # Classification storage (per-counterexample validation result)
    # ------------------------------------------------------------------

    def save_classification(self, driver: str, function: str, result: Any) -> Path:
        """Save a ValidationResult. Each (function, failing_property) pair gets
        its own historical record so multi-CEx functions don't overwrite
        earlier classifications.

        Layout mirrors save_bug_report:
          ``{driver}/{function}/classification.json``                     — latest CEx
          ``{driver}/{function}/classifications/<property_safe>.json``    — per-CEx history
        """
        fn_dir = self._fn_dir(driver, function)
        path = fn_dir / "classification.json"
        payload: dict[str, Any] = {"saved_at": _utcnow()}
        if hasattr(result, "to_dict"):
            payload["classification"] = result.to_dict()
        elif isinstance(result, dict):
            payload["classification"] = result
        elif hasattr(result, "__dataclass_fields__"):
            import dataclasses
            payload["classification"] = dataclasses.asdict(result)
        else:
            payload["classification"] = str(result)
        _write_json(path, payload)
        try:
            c = payload.get("classification") or {}
            prop = ""
            if isinstance(c, dict):
                cex = c.get("counterexample") or {}
                prop = (cex.get("failing_property") if isinstance(cex, dict) else "") or ""
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(prop))[:120] \
                   or f"unnamed_{int(time.time()*1000)}"
            (fn_dir / "classifications").mkdir(parents=True, exist_ok=True)
            _write_json(fn_dir / "classifications" / f"{safe}.json", payload)
        except Exception:
            pass
        return path

    def load_classification(self, driver: str, function: str) -> Optional[dict]:
        path = self._fn_dir(driver, function) / "classification.json"
        if not path.exists():
            return None
        return _read_json(path).get("classification")

    # ------------------------------------------------------------------
    # Refinement history storage
    # ------------------------------------------------------------------

    def save_refinement_history(
        self,
        driver: str,
        function: str,
        history: list[dict[str, Any]],
    ) -> Path:
        """Save the refinement iteration history to ``{driver}/{function}/refinement_history.json``."""
        path = self._fn_dir(driver, function) / "refinement_history.json"
        payload = {"saved_at": _utcnow(), "refinement_history": history}
        _write_json(path, payload)
        return path

    def load_refinement_history(self, driver: str, function: str) -> Optional[list]:
        path = self._fn_dir(driver, function) / "refinement_history.json"
        if not path.exists():
            return None
        return _read_json(path).get("refinement_history")

    # ------------------------------------------------------------------
    # Propagation events storage
    # ------------------------------------------------------------------

    def save_propagation_events(
        self,
        driver: str,
        function: str,
        events: list[Any],
    ) -> Path:
        """Save PropagationEvent list to ``{driver}/{function}/propagation_events.json``."""
        path = self._fn_dir(driver, function) / "propagation_events.json"
        import dataclasses
        serialized = []
        for e in events:
            if hasattr(e, "__dataclass_fields__"):
                serialized.append(dataclasses.asdict(e))
            elif isinstance(e, dict):
                serialized.append(e)
            else:
                serialized.append(str(e))
        payload = {"saved_at": _utcnow(), "propagation_events": serialized}
        _write_json(path, payload)
        return path

    def load_propagation_events(self, driver: str, function: str) -> Optional[list]:
        path = self._fn_dir(driver, function) / "propagation_events.json"
        if not path.exists():
            return None
        return _read_json(path).get("propagation_events")

    # ------------------------------------------------------------------
    # Spec quality storage
    # ------------------------------------------------------------------

    def save_spec_quality(self, driver: str, function: str, report: Any) -> Path:
        """Save a SpecQualityReport to ``{driver}/{function}/spec_quality.json``."""
        path = self._fn_dir(driver, function) / "spec_quality.json"
        payload: dict[str, Any] = {
            "saved_at": _utcnow(),
        }
        if isinstance(report, dict):
            payload["report"] = report
        elif hasattr(report, "to_dict"):
            payload["report"] = report.to_dict()
        elif hasattr(report, "__dataclass_fields__"):
            import dataclasses
            payload["report"] = dataclasses.asdict(report)
        else:
            payload["report"] = str(report)
        _write_json(path, payload)
        return path

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def get_run_summary(self, driver: str) -> dict[str, Any]:
        """
        Return aggregate statistics for a driver.

        Counts how many functions have specs, CBMC results, and bug reports,
        and tallies SpecStatus values.
        """
        driver_dir = self.base_dir / driver
        if not driver_dir.exists():
            return {"driver": driver, "error": "driver directory not found"}

        stats: dict[str, Any] = {
            "driver": driver,
            "functions": [],
            "total": 0,
            "with_spec": 0,
            "with_cbmc_result": 0,
            "with_bug_report": 0,
            "spec_status_counts": {s.value: 0 for s in SpecStatus},
        }

        for fn_dir in sorted(driver_dir.iterdir()):
            if not fn_dir.is_dir():
                continue
            fn_name = fn_dir.name
            fn_info: dict[str, Any] = {"function": fn_name}

            spec_path = fn_dir / "spec.json"
            cbmc_path = fn_dir / "cbmc_result.json"
            bug_path = fn_dir / "bug_report.json"
            cls_path = fn_dir / "classification.json"
            ref_path = fn_dir / "refinement_history.json"
            prop_path = fn_dir / "propagation_events.json"

            has_spec = spec_path.exists()
            has_cbmc = cbmc_path.exists()
            has_bug = bug_path.exists()

            fn_info["has_spec"] = has_spec
            fn_info["has_cbmc_result"] = has_cbmc
            fn_info["has_bug_report"] = has_bug
            fn_info["has_classification"] = cls_path.exists()
            fn_info["has_refinement_history"] = ref_path.exists()
            fn_info["has_propagation_events"] = prop_path.exists()

            if has_spec:
                stats["with_spec"] += 1
                try:
                    spec_data = _read_json(spec_path)
                    status_val = spec_data["spec"].get("status", "pending")
                    fn_info["spec_status"] = status_val
                    stats["spec_status_counts"][status_val] = (
                        stats["spec_status_counts"].get(status_val, 0) + 1
                    )
                except Exception:
                    fn_info["spec_status"] = "unknown"

            if has_cbmc:
                stats["with_cbmc_result"] += 1
                try:
                    cbmc_data = _read_json(cbmc_path)
                    fn_info["cbmc_verified"] = cbmc_data.get("result", {}).get(
                        "verified", None
                    )
                except Exception:
                    pass

            if has_bug:
                stats["with_bug_report"] += 1

            stats["functions"].append(fn_info)
            stats["total"] += 1

        return stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
