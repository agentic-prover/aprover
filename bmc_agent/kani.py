"""
Kani (Rust bounded model checker) subprocess wrapper for BMC-Agent.

Mirrors :mod:`bmc_agent.cbmc`: runs Kani on a Rust harness file, parses its
output, and returns a :class:`bmc_agent.cbmc.CBMCResult`.  Reusing the
existing result type means the rest of the pipeline (CEx classifier,
artifact store, bug reporter) is backend-agnostic.

Kani is a separate tool that itself uses CBMC under the hood.  Its output
format is similar to CBMC's but not identical, so the parser here looks
for both Kani-specific markers (``VERIFICATION:- SUCCESSFUL``,
``Failed Checks:``) and CBMC-style ones.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from bmc_agent.cbmc import CBMCResult, Counterexample


# Kani's standalone CLI invocation form:
#   kani <file.rs> [--harness <name>] [--default-unwind N]
# When the path is a Cargo project we'd use `cargo kani` instead, but the
# pipeline drops single .rs files into a working dir per function so single-file
# mode is the natural fit.
#
# We use Kani's default ("regular") output format, not "old": the old format
# emits per-assertion reachability_check rows that report FAILURE when the
# assertion *was reached* (i.e. healthy proofs), and its per-harness summary
# prints "VERIFICATION FAILED" for any harness containing such a row. That
# inverts the verdict and is essentially unparseable. Regular format suppresses
# reachability checks and prints "VERIFICATION:- SUCCESSFUL/FAILED" cleanly.


def run_kani(
    harness_path: str | Path,
    harness_name: str | None = None,
    unwind: int = 4,
    timeout: int = 120,
    kani_path: str = "kani",
) -> CBMCResult:
    """
    Run Kani on *harness_path* and return a structured result.

    Parameters
    ----------
    harness_path:
        Path to a self-contained Rust harness file containing one or more
        ``#[kani::proof]`` functions.
    harness_name:
        Optional name of a single ``#[kani::proof]`` function to verify.
        When omitted, Kani verifies every proof it finds.
    unwind:
        Default loop unwinding bound (``--default-unwind N``).
    timeout:
        Maximum wall-clock seconds before the process is killed.
    kani_path:
        Path / name of the Kani executable.

    Returns
    -------
    CBMCResult
        On Kani-not-found: ``CBMCResult(verified=False, error="kani not found")``.
        On timeout:        ``CBMCResult(verified=False, error="kani timed out")``.
        Otherwise a parsed verdict with any counterexamples extracted from
        the textual output.
    """
    harness_path = Path(harness_path)

    if not shutil.which(kani_path):
        return CBMCResult(verified=False, error="kani not found")

    cmd: list[str] = [
        kani_path,
        str(harness_path),
        "--default-unwind",
        str(unwind),
    ]
    if harness_name:
        cmd += ["--harness", harness_name]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CBMCResult(verified=False, error=f"kani timed out after {timeout}s")
    except FileNotFoundError:
        return CBMCResult(verified=False, error="kani not found")
    except OSError as exc:
        return CBMCResult(verified=False, error=f"kani OS error: {exc}")

    raw = proc.stdout or ""
    stderr = proc.stderr or ""
    return _parse_kani_output(raw, stderr, proc.returncode)


def find_crate_root(source_path: str | Path) -> Optional[Path]:
    """Walk up from *source_path* looking for the nearest ``Cargo.toml`` --
    that's the crate root. Returns ``None`` if no Cargo.toml is found
    (e.g. ad-hoc .rs file outside a crate).
    """
    p = Path(source_path).resolve()
    if p.is_file():
        p = p.parent
    while p != p.parent:
        if (p / "Cargo.toml").exists():
            return p
        p = p.parent
    return None


def _extract_harness_proof_block(harness_src: str) -> str:
    """Pull the ``#[kani::proof]`` function (and any preceding ``use`` or
    helper items needed) out of a standalone harness file so it can be
    appended to the function-under-test's source file in cargo-mode.

    We keep:
      * The ``#[cfg(kani)] #[kani::proof] fn check_<fn>() { ... }`` block.
      * Any ``use`` statements that survived the standalone strip pass --
        in cargo-mode all crate-local types are already in scope through
        the host file, so we generally don't need these, but harmless to
        keep when they reference std/core/alloc.

    We DROP:
      * The inlined source the standalone harness copied in (the host file
        already has it).
      * The harness file's preamble (`#![allow(...)]` etc.).
    """
    # Take everything from the first `#[kani::proof]` annotation to EOF.
    idx = harness_src.find("#[kani::proof]")
    if idx == -1:
        return harness_src  # fallback: append the whole thing
    # Walk back to the start of the line that contains the annotation.
    line_start = harness_src.rfind("\n", 0, idx) + 1
    block = harness_src[line_start:]
    # Drop trailing whitespace.
    return block.rstrip() + "\n"


def run_kani_cargo(
    crate_root: str | Path,
    source_path: str | Path,
    harness_src: str,
    harness_name: str,
    unwind: int = 4,
    timeout: int = 120,
) -> CBMCResult:
    """Run Kani on a harness appended to its function-under-test's source file.

    *crate_root* is the directory containing ``Cargo.toml``. *source_path*
    is the original .rs file containing the function-under-test; the
    harness is appended to this file so it can access private items in
    the function's module. *harness_src* is the full standalone-harness
    text (this function extracts just the proof block to append).
    *harness_name* is the ``#[kani::proof]`` function name.

    Mirrors the C-side ``cbmc_real_libc`` mode: instead of generating a
    standalone compilation unit, we let cargo's existing build context
    resolve all imports and visibility, then run ``cargo kani --harness
    <name>`` from the crate root. The original file is snapshotted and
    restored atomically afterwards so concurrent runs and Ctrl-C never
    leave the host crate modified.
    """
    import shutil as _shutil
    import subprocess as _sub

    crate_root = Path(crate_root)
    source_path = Path(source_path)

    if not _shutil.which("cargo"):
        return CBMCResult(verified=False, error="cargo not found")
    if not _shutil.which("cargo-kani"):
        return CBMCResult(verified=False, error="cargo-kani not found")
    if not source_path.is_file():
        return CBMCResult(verified=False, error=f"source file not found: {source_path}")

    # Snapshot the original source. The append+restore pattern is atomic
    # against unexpected exits because we always restore in the finally
    # block; concurrent appends on the same file (different harnesses on
    # different functions in the same .rs) are NOT safe — the caller must
    # serialise per-file in that case.
    #
    # Cleanup on startup: strip any leftover bmc_agent sentinel blocks
    # before snapshotting. Interrupted runs (SIGKILL, OOM, lost ssh) can
    # leave stale harnesses in the source -- those poison subsequent
    # cargo compiles for unrelated functions. We strip them so each run
    # starts from a known-clean source state. Sentinels look like:
    #   // === bmc_agent cargo-kani harness <NAME> -- DO NOT EDIT ===
    #   ... proof block ...
    #   // === end bmc_agent harness <NAME> ===
    import re as _re
    raw_bytes = source_path.read_bytes()
    cleaned_bytes = _re.sub(
        rb"\n?// === bmc_agent cargo-kani harness [^\n]+ -- DO NOT EDIT ===.*?// === end bmc_agent harness [^\n]+ ===\n?",
        b"",
        raw_bytes,
        flags=_re.DOTALL,
    )
    if cleaned_bytes != raw_bytes:
        # Salvage: write the cleaned bytes to disk so even if THIS run is
        # interrupted, the next one starts from the cleaned state.
        source_path.write_bytes(cleaned_bytes)
    original_bytes = cleaned_bytes

    proof_block = _extract_harness_proof_block(harness_src)
    sentinel_start = f"\n// === bmc_agent cargo-kani harness {harness_name} -- DO NOT EDIT ===\n"
    sentinel_end = f"\n// === end bmc_agent harness {harness_name} ===\n"
    appended = original_bytes + sentinel_start.encode() + proof_block.encode() + sentinel_end.encode()
    try:
        source_path.write_bytes(appended)
        try:
            proc = _sub.run(
                ["cargo", "kani", "--harness", harness_name,
                 "--default-unwind", str(unwind)],
                cwd=str(crate_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except _sub.TimeoutExpired:
            return CBMCResult(verified=False, error=f"cargo kani timed out after {timeout}s")
        except FileNotFoundError:
            return CBMCResult(verified=False, error="cargo or kani not found")
        except OSError as exc:
            return CBMCResult(verified=False, error=f"cargo kani OS error: {exc}")
        raw = proc.stdout or ""
        stderr = proc.stderr or ""
        return _parse_kani_output(raw, stderr, proc.returncode)
    finally:
        # Always restore -- atomic write to avoid partial-restore on
        # crash during recovery itself.
        try:
            source_path.write_bytes(original_bytes)
        except OSError:
            # Last-resort warning so the user knows a manual rollback may
            # be needed; can't raise here (we're in finally).
            import logging as _logging
            _logging.getLogger("bmc_agent.kani").error(
                "Failed to restore %s after cargo-kani run", source_path,
            )


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


# Kani's regular (default) output format. The per-harness verdict is a single
# "VERIFICATION:- SUCCESSFUL" or "VERIFICATION:- FAILED" line; each property is
# a multi-line "Check N: <id> / Status: ... / Description: ..." block.
_VERDICT_SUCCESS_RE = re.compile(r"^VERIFICATION:-\s*SUCCESSFUL", re.MULTILINE)
_VERDICT_FAILED_RE = re.compile(r"^VERIFICATION:-\s*FAILED", re.MULTILINE)

# Per-check failure block in regular format. The `Check N:` header line is
# followed by indented "- Status:" and "- Description:" lines. Status FAILURE
# is the genuine failure signal — reachability_check rows are not present in
# regular output.
_REGULAR_CHECK_FAILURE_RE = re.compile(
    r"^Check\s+\d+:\s*(?P<prop>\S+)\s*\n"
    r"\s*-\s*Status:\s*FAILURE\s*\n"
    r"\s*-\s*Description:\s*\"?(?P<desc>[^\"\n]*)\"?",
    re.MULTILINE,
)

# Legacy/old-format per-property failure row, kept for backward compatibility
# with synthetic test fixtures and other Kani output variants.
_LEGACY_PROPERTY_FAILURE_RE = re.compile(
    r"^\s*\[([^\]]+)\]\s*(.*?):\s*(?:FAILURE|FAILED)\s*$",
    re.MULTILINE,
)

# Kani sometimes summarises failures as "Failed Checks: <description>".
_FAILED_CHECKS_RE = re.compile(r"^Failed Checks:\s*(.+)$", re.MULTILINE)

# Loop unwinding assertion: reported when a loop runs past Kani's
# unwind bound.  The result is inconclusive, not a real CEx — bumping
# the bound or proving the loop bounded by other means is needed to
# conclude verification.  Pattern matches both formats.
_REGULAR_UNWIND_RE = re.compile(
    r"^Check\s+\d+:\s*(?P<prop>\S+\.unwind\.\d+)\s*\n"
    r"\s*-\s*Status:\s*FAILURE\s*\n"
    r"\s*-\s*Description:\s*\"?(?P<desc>[^\"\n]*)\"?",
    re.MULTILINE,
)
_LEGACY_UNWIND_RE = re.compile(
    r"^\s*\[([^\]]*\.unwind\.\d+)\]\s*(.*?):\s*(?:FAILURE|FAILED)\s*$",
    re.MULTILINE,
)


def _extract_unwind_failures(raw: str) -> list[str]:
    """Return short descriptions of every unwind-assertion failure in
    *raw*.  An empty list means no loop hit its unwind bound."""
    descriptions: list[str] = []
    seen: set[str] = set()
    for match in _REGULAR_UNWIND_RE.finditer(raw):
        prop = match.group("prop").strip()
        desc = (match.group("desc") or "").strip() or prop
        if prop in seen:
            continue
        seen.add(prop)
        descriptions.append(desc)
    for match in _LEGACY_UNWIND_RE.finditer(raw):
        prop = match.group(1).strip()
        desc = (match.group(2) or "").strip() or prop
        if prop in seen:
            continue
        seen.add(prop)
        descriptions.append(desc)
    return descriptions


def _parse_kani_output(raw: str, stderr: str, returncode: int) -> CBMCResult:
    """Parse Kani text output into a CBMCResult.

    Kani's exit codes match CBMC's convention: 0 = verified, non-zero =
    failed or error.  When the run produced no verdict marker at all we
    treat it as an error.

    Three failure modes need to be distinguished:

      * Real property violation — a ``.assertion.N`` row marked FAILURE.
        Reported as ``verified=False`` with the counterexample attached.
      * Reachability check failure — a ``.reachability_check.N`` row;
        these report FAILURE when the assertion was *reached* (a healthy
        signal) so they are silently ignored.
      * Unwinding-assertion failure — a ``.unwind.N`` row; Kani's loop
        ran past the unwind bound and the run is inconclusive (we
        cannot conclude the property holds OR fails).  Surface this as
        ``verified=False`` with an ``error`` describing the unwind bound
        rather than as a fake counterexample.
    """
    has_success = bool(_VERDICT_SUCCESS_RE.search(raw))
    has_failure = bool(_VERDICT_FAILED_RE.search(raw))

    if not has_success and not has_failure:
        # No verdict at all → treat as error and surface stderr.
        err = (stderr or "").strip() or f"kani exited with code {returncode}"
        return CBMCResult(verified=False, raw_output=raw, error=err)

    counterexamples = _extract_counterexamples(raw)
    unwind_failures = _extract_unwind_failures(raw)

    if counterexamples:
        # A real property failure dominates: report the CEx and ignore
        # any concurrent unwind warnings.
        return CBMCResult(verified=False, counterexamples=counterexamples, raw_output=raw)

    if unwind_failures:
        # Inconclusive: loop unwind bound exhausted, no real CEx found.
        # Synthesise an error so the verdict surfaces clearly.
        first = unwind_failures[0]
        return CBMCResult(
            verified=False,
            counterexamples=[],
            raw_output=raw,
            error=f"loop unwind bound exhausted: {first}",
        )

    # No CEx, no unwind issue → verified iff Kani said SUCCESSFUL.
    return CBMCResult(verified=has_success, counterexamples=[], raw_output=raw)


def _extract_counterexamples(raw: str) -> list[Counterexample]:
    """Pull failing properties out of Kani's text output.

    Tries the regular-format multi-line ``Check N:`` blocks first, then
    falls back to the legacy ``[id] desc: FAILURE`` row form, then to a
    single ``Failed Checks:`` summary line. ``.reachability_check.N``
    rows (a Kani internal: FAILURE means the assertion was *reached*) and
    ``.unwind.N`` rows (loop ran past the unwind bound; inconclusive,
    not a real CEx) are filtered out — both would otherwise be reported
    as bogus property violations.
    """
    cexes: list[Counterexample] = []
    seen: set[str] = set()

    for match in _REGULAR_CHECK_FAILURE_RE.finditer(raw):
        prop_id = match.group("prop").strip()
        if ".unwind." in prop_id or ".reachability_check." in prop_id:
            continue
        desc = match.group("desc").strip()
        if prop_id in seen:
            continue
        seen.add(prop_id)
        trace = [f"property {prop_id}: {desc}"] if desc else []
        cexes.append(
            Counterexample(
                failing_property=prop_id,
                variable_assignments={},
                trace=trace,
            )
        )

    if not cexes:
        for match in _LEGACY_PROPERTY_FAILURE_RE.finditer(raw):
            prop_id = match.group(1).strip()
            # Skip reachability_check and unwind pseudo-failures —
            # neither indicates a real property violation.
            if ".reachability_check." in prop_id or ".unwind." in prop_id:
                continue
            desc = match.group(2).strip()
            if prop_id in seen:
                continue
            seen.add(prop_id)
            trace = [f"property {prop_id}: {desc}"] if desc else []
            cexes.append(
                Counterexample(
                    failing_property=prop_id,
                    variable_assignments={},
                    trace=trace,
                )
            )

    if not cexes:
        m = _FAILED_CHECKS_RE.search(raw)
        if m:
            desc = m.group(1).strip()
            # The "Failed Checks:" summary line may reflect a real CEx
            # or an inconclusive unwind failure; if it's the latter,
            # leave the cex list empty so _parse_kani_output surfaces
            # the unwind failure via its dedicated path.
            if "unwinding" not in desc.lower():
                cexes.append(
                    Counterexample(
                        failing_property="failed_checks",
                        variable_assignments={},
                        trace=[desc],
                    )
                )

    return cexes
