"""
Adjacent-bug follower.

When the realism check rejects a CBMC counterexample as a harness artifact but
notices a *different* exploitable defect nearby (in the same function, a callee,
or a related function), it records it in ``realism_check.adjacent_bugs[]``. This
module is the consumer: after a sweep finishes, walk every ``bug_report.json``,
collect those leads, dedup by ``(source_file, target_function)``, and run a
follow-up bmc-agent pipeline pass on each unique source file. Round-N outputs
land in ``<sweep_output>/adjacent_round_N/`` so they can be aggregated alongside
the original findings without overwriting anything.

There is NO human in this loop: the LLM produces leads, this module consumes
them, the pipeline re-verifies them, and the resulting bug_reports flow into
the next round (capped by ``rounds``). Side-cars become inputs to subsequent
runs instead of files no one reads.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _function_name_from_location(loc: str) -> str | None:
    """Extract a function name from common location formats.

    Accepts:
      ``foo.c:123``                  -> None (no function specified)
      ``foo_bar:123``                -> "foo_bar"
      ``foo_bar``                    -> "foo_bar"
      ``foo.c::foo_bar:123``         -> "foo_bar"
      ``foo_bar (foo.c:123)``        -> "foo_bar"
    """
    if not loc:
        return None
    loc = loc.strip()
    # Try "<word> (...)" form first.
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[\(\[]", loc)
    if m:
        return m.group(1)
    # Try "<path>::<func>[:line]" form.
    if "::" in loc:
        right = loc.split("::", 1)[1]
        right = right.split(":", 1)[0].strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", right):
            return right
    # Try "<func>:<line>" form (no path separators).
    if "/" not in loc and "\\" not in loc and ":" in loc:
        left = loc.split(":", 1)[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", left):
            return left
    # Plain identifier.
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", loc):
        return loc
    return None


def collect_adjacent_bugs(sweep_output: Path) -> list[dict]:
    """Walk every ``bug_report.json`` under ``sweep_output`` and return a flat
    list of adjacent-bug leads. Each lead is a dict with keys:

      ``source_function``  — the function whose realism check reported it
      ``source_file_stem`` — the .c-file-stem directory in the sweep
      ``location``         — raw LLM-supplied location string
      ``target_function``  — extracted function name (may be None)
      ``bug_type``         — LLM-supplied
      ``attacker_scenario``— LLM-supplied
      ``confidence``       — LLM-supplied
      ``provenance_path``  — path to the originating bug_report.json
    """
    leads: list[dict] = []
    for br_path in sweep_output.rglob("bug_report.json"):
        # Skip outputs we ourselves produced in previous rounds; only follow
        # adjacency starting from the original primary findings to keep blast
        # radius bounded. Recursion across rounds is opt-in.
        if "adjacent_round_" in br_path.as_posix():
            continue
        try:
            with open(br_path) as f:
                doc = json.load(f)
        except Exception as e:
            logger.warning("Could not read %s: %s", br_path, e)
            continue
        report = doc.get("report") or {}
        rc = report.get("realism_check") or {}
        for entry in (rc.get("adjacent_bugs") or []):
            if not isinstance(entry, dict):
                continue
            loc = str(entry.get("location", "")).strip()
            scenario = str(entry.get("attacker_scenario", "")).strip()
            if not scenario:
                continue
            leads.append({
                "source_function": report.get("function_name", ""),
                "source_file_stem": br_path.parent.parent.name,
                "location": loc,
                "target_function": _function_name_from_location(loc),
                "bug_type": str(entry.get("bug_type", "")).strip(),
                "attacker_scenario": scenario,
                "confidence": str(entry.get("confidence", "")).strip(),
                "provenance_path": br_path.as_posix(),
            })
    return leads


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def dedup_leads(leads: Iterable[dict]) -> list[dict]:
    """Dedup by ``(source_file_stem, target_function)``. Keep the highest-
    confidence variant. Drop leads with no target_function (we can't act
    on them without a function to harness).
    """
    by_key: dict[tuple[str, str], dict] = {}
    confidence_rank = {"high": 3, "medium": 2, "low": 1, "": 0}
    for lead in leads:
        target = lead.get("target_function") or ""
        if not target:
            continue
        key = (lead["source_file_stem"], target)
        prev = by_key.get(key)
        if (
            prev is None
            or confidence_rank.get(lead.get("confidence", ""), 0)
            > confidence_rank.get(prev.get("confidence", ""), 0)
        ):
            by_key[key] = lead
    return list(by_key.values())


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def _resolve_source_file(source_dir: Path, file_stem: str) -> Path | None:
    """Map a sweep sub-directory name back to a .c file in the corpus."""
    candidate = source_dir / f"{file_stem}.c"
    if candidate.exists():
        return candidate
    # Some sweeps name dirs after the .c basename minus extension; if the .c
    # file lives under a subdir, scan once.
    for c in source_dir.rglob(f"{file_stem}.c"):
        return c
    return None


def spawn_round(
    leads: list[dict],
    source_dir: Path,
    sweep_output: Path,
    config,
    round_num: int,
) -> dict[str, list]:
    """For each unique source file referenced by ``leads``, re-run the pipeline.

    Round-N artifacts go to ``<sweep_output>/adjacent_round_<round_num>/``.
    The pipeline re-verifies all functions in the file; the new bug_reports
    naturally include realism re-checks that may surface further adjacent
    bugs, which a subsequent round can pick up. Returns
    ``{driver_name: [BugReport, ...]}``.
    """
    from bmc_agent.pipeline import AMCPipeline

    round_dir = sweep_output / f"adjacent_round_{round_num}"
    round_dir.mkdir(parents=True, exist_ok=True)

    # Persist the leads we're acting on so the round is auditable.
    with open(round_dir / "leads.json", "w") as f:
        json.dump(leads, f, indent=2)

    # Dedup further by file (we already deduped by (file, target_function)).
    files_to_run = sorted({lead["source_file_stem"] for lead in leads})

    # Use a sub-artifact dir so previous outputs are untouched.
    config.artifact_dir = round_dir.as_posix()
    pipeline = AMCPipeline(config)
    results: dict[str, list] = {}

    # Group leads by source-file stem so we can pass per-file function_hints
    # in one pipeline call (the pipeline re-runs on the whole file; hints
    # only apply to functions in that file). Each hint joins all
    # attacker_scenarios for the same target_function with a blank line
    # separator so multiple round-(N-1) reporters' hypotheses survive.
    hints_by_file: dict[str, dict[str, str]] = {}
    for lead in leads:
        stem = lead["source_file_stem"]
        target = lead["target_function"]
        scenario = lead["attacker_scenario"]
        per_file = hints_by_file.setdefault(stem, {})
        if target in per_file:
            per_file[target] = per_file[target] + "\n\n" + scenario
        else:
            per_file[target] = scenario

    for stem in files_to_run:
        src = _resolve_source_file(source_dir, stem)
        if src is None:
            logger.warning("Round %d: source file for stem '%s' not found in %s",
                           round_num, stem, source_dir)
            continue
        driver = f"adjacent_round_{round_num}/{stem}"
        function_hints = hints_by_file.get(stem, {})
        logger.info(
            "Round %d: re-running pipeline on %s (driver=%s, %d hint(s))",
            round_num, src, driver, len(function_hints),
        )
        try:
            bugs = pipeline.run(
                source_file=src.as_posix(),
                driver_name=driver,
                function_hints=function_hints,
            )
            results[driver] = bugs
        except Exception as exc:
            logger.warning("Round %d: pipeline crashed on %s: %s",
                           round_num, src, exc)
            results[driver] = []

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _collect_confirmed_functions(scan_root: Path) -> set[tuple[str, str]]:
    """Walk every bug_report.json under ``scan_root`` and return the set of
    ``(source_file_stem, function_name)`` pairs whose realism check is REALISTIC.
    Used by (B) to dedup adjacent-bug leads against already-confirmed bugs so
    we don't re-verify the same function as both an adjacent lead and an
    already-confirmed finding.
    """
    confirmed: set[tuple[str, str]] = set()
    for br_path in scan_root.rglob("bug_report.json"):
        try:
            with open(br_path) as f:
                doc = json.load(f)
        except Exception:
            continue
        report = doc.get("report") or {}
        rc = report.get("realism_check") or {}
        if (rc.get("verdict") or "").lower() != "realistic":
            continue
        stem = br_path.parent.parent.name
        fn = report.get("function_name", "")
        if stem and fn:
            confirmed.add((stem, fn))
    return confirmed


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}


def _prioritize_leads(leads: list[dict]) -> list[dict]:
    """(C) Sort leads so HIGH-confidence ones get processed first. Within
    confidence rank, sort by source_file_stem for stable ordering. We then
    group by file (spawn_round dedups by file), but the file-order itself
    follows whichever file contains the highest-confidence lead.
    """
    def key(lead):
        return (
            -_CONFIDENCE_RANK.get(lead.get("confidence", ""), 0),
            lead.get("source_file_stem", ""),
            lead.get("target_function", ""),
        )
    return sorted(leads, key=key)


def follow_rounds(
    source_dir: Path,
    sweep_output: Path,
    config,
    rounds: int = 1,
) -> dict[int, dict[str, list]]:
    """Top-level driver. Repeats collect → dedup → spawn for up to ``rounds``
    iterations. After round-N completes, its bug_reports are scanned for
    further adjacent bugs and round-(N+1) processes any new ones.

    (B) Cross-round dedup against already-confirmed REALISTIC findings —
        leads pointing to a function we've already confirmed don't get
        re-verified.
    (C) Lead prioritization by confidence — HIGH-confidence leads run first
        so if we hit a time/cost cap, the best leads got their chance.

    Returns ``{round_num: {driver: [BugReport, ...]}}``.
    """
    out: dict[int, dict[str, list]] = {}
    seen_keys: set[tuple[str, str]] = set()
    cur_output = sweep_output
    # (B) Seed seen_keys with everything REALISTIC found in round-0 so
    # round-1 doesn't re-verify them. Each round below also folds in its
    # own confirmed findings before the next round.
    confirmed = _collect_confirmed_functions(cur_output)
    seen_keys |= confirmed
    logger.info("Adjacent follower: %d already-confirmed (file, fn) pairs "
                "will be excluded from leads", len(confirmed))
    for r in range(1, rounds + 1):
        all_leads = collect_adjacent_bugs(cur_output)
        deduped = dedup_leads(all_leads)
        fresh = [
            lead for lead in deduped
            if (lead["source_file_stem"], lead["target_function"]) not in seen_keys
        ]
        if not fresh:
            logger.info("Round %d: no new adjacent-bug leads — stopping", r)
            break
        # (C) Prioritize by confidence — HIGH first.
        fresh = _prioritize_leads(fresh)
        logger.info(
            "Round %d: %d fresh leads (from %d total adjacent reports; "
            "%d already-seen targets filtered)",
            r, len(fresh), len(all_leads),
            len(deduped) - len(fresh),
        )
        for lead in fresh:
            seen_keys.add((lead["source_file_stem"], lead["target_function"]))
        out[r] = spawn_round(fresh, source_dir, sweep_output, config, r)
        # After this round, fold in its confirmed findings so the next round
        # skips them.
        round_dir = sweep_output / f"adjacent_round_{r}"
        new_confirmed = _collect_confirmed_functions(round_dir)
        seen_keys |= new_confirmed
        cur_output = round_dir
    return out
