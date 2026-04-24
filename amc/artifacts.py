"""
Artifact directory layout for GRACE.

One subdirectory per driver; one sub-subdirectory per function.
Each result is saved as a JSON file inside the function directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from amc.spec import Spec, SpecStatus


class ArtifactStore:
    """
    Manages the on-disk artifact layout for a GRACE verification run.

    Layout::

        {base_dir}/
            grace.log
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
        """Save a bug report to ``{driver}/{function}/bug_report.json``."""
        path = self._fn_dir(driver, function) / "bug_report.json"
        payload: dict[str, Any] = {
            "saved_at": _utcnow(),
        }
        if isinstance(report, dict):
            payload["report"] = report
        elif hasattr(report, "__dataclass_fields__"):
            import dataclasses

            payload["report"] = dataclasses.asdict(report)
        else:
            payload["report"] = str(report)
        _write_json(path, payload)
        return path

    def load_bug_report(self, driver: str, function: str) -> Optional[dict]:
        """Load a bug report dict from disk."""
        path = self._fn_dir(driver, function) / "bug_report.json"
        if not path.exists():
            return None
        return _read_json(path).get("report")

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

            has_spec = spec_path.exists()
            has_cbmc = cbmc_path.exists()
            has_bug = bug_path.exists()

            fn_info["has_spec"] = has_spec
            fn_info["has_cbmc_result"] = has_cbmc
            fn_info["has_bug_report"] = has_bug

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
