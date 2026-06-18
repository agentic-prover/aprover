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

    import os as _os0, signal as _sig0
    try:
        _pp = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except FileNotFoundError:
        return CBMCResult(verified=False, error="kani not found")
    except OSError as exc:
        return CBMCResult(verified=False, error=f"kani OS error: {exc}")
    try:
        _o, _e = _pp.communicate(timeout=timeout)
        proc = subprocess.CompletedProcess(_pp.args, _pp.returncode, _o, _e)
    except subprocess.TimeoutExpired:
        try:
            _os0.killpg(_os0.getpgid(_pp.pid), _sig0.SIGKILL)
        except Exception:
            pass
        try:
            _pp.wait(timeout=10)
        except Exception:
            pass
        return CBMCResult(verified=False, error=f"kani timed out after {timeout}s")

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


import threading as _threading

# Per-crate-root threading locks. fcntl.flock serializes between *processes*,
# but Linux BSD-flock semantics between two fds in the same process are
# advisory-and-ambiguous: two threads each open()ing the lockfile and calling
# flock(LOCK_EX) on independent fds can both succeed and end up writing the
# source file concurrently, accumulating multiple #[kani::proof] blocks.
# A per-crate threading.Lock guarantees in-process serialization regardless.
_CRATE_THREAD_LOCKS: dict[str, "_threading.Lock"] = {}
_CRATE_THREAD_LOCKS_GUARD = _threading.Lock()


def _acquire_crate_lock(crate_root: "Path"):
    """Acquire BOTH a process-local threading.Lock AND an fcntl flock on
    ``<crate_root>/.bmc_agent.lock`` so only one cargo-kani runs against
    a crate at a time, regardless of (a) how many threads are in this
    bmc-agent process and (b) how many bmc-agent processes are active.

    Returns a (thread_lock, fh) tuple; the caller passes the whole tuple
    back to _release_crate_lock. Returns (None, None) on platforms
    without fcntl -- threading.Lock alone still serializes the
    in-process side.
    """
    key = str(crate_root.resolve())
    with _CRATE_THREAD_LOCKS_GUARD:
        tlock = _CRATE_THREAD_LOCKS.get(key)
        if tlock is None:
            tlock = _threading.Lock()
            _CRATE_THREAD_LOCKS[key] = tlock
    tlock.acquire()  # blocks until this thread owns the crate
    try:
        import fcntl
    except ImportError:
        return (tlock, None)  # Windows: threading lock alone
    lock_path = crate_root / ".bmc_agent.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        fh.close()
        return (tlock, None)
    return (tlock, fh)


def _release_crate_lock(handle) -> None:
    if handle is None:
        return
    tlock, fh = handle
    if fh is not None:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass
    if tlock is not None:
        try:
            tlock.release()
        except Exception:
            pass


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

    # Snapshot + restore strategy: prefer `git checkout` when the crate is
    # in a git repo (which is the case for everything we clone), because it
    # handles the FULL repo state — not just the one source file. Stale
    # appended harnesses in OTHER files (from prior crashed runs) also get
    # restored. Falls back to byte-level snapshot when the crate is not a
    # git repo.
    import shutil as _shutil2
    import subprocess as _sub2
    use_git = False
    if (crate_root / ".git").exists() and _shutil2.which("git"):
        # Verify git status works (catches submodule weirdness, etc.)
        st = _sub2.run(
            ["git", "status", "--porcelain"], cwd=str(crate_root),
            capture_output=True, text=True, timeout=10,
        )
        if st.returncode == 0:
            use_git = True
            # Restore any pre-existing modifications before we snapshot --
            # interrupted prior runs may have left things dirty.
            _sub2.run(
                ["git", "checkout", "--", "."], cwd=str(crate_root),
                capture_output=True, timeout=30,
            )

    # Re-read after potential git checkout.
    original_bytes = source_path.read_bytes()

    proof_block = _extract_harness_proof_block(harness_src)
    sentinel_start = f"\n// === bmc_agent cargo-kani harness {harness_name} -- DO NOT EDIT ===\n"
    sentinel_end = f"\n// === end bmc_agent harness {harness_name} ===\n"
    appended = original_bytes + sentinel_start.encode() + proof_block.encode() + sentinel_end.encode()
    crate_lock = _acquire_crate_lock(crate_root)
    try:
        source_path.write_bytes(appended)
        try:
            import os as _os, signal as _signal
            _popen = _sub.Popen(
                ["cargo", "kani", "--harness", harness_name,
                 "--default-unwind", str(unwind)],
                cwd=str(crate_root),
                stdout=_sub.PIPE,
                stderr=_sub.PIPE,
                text=True,
                start_new_session=True,  # own process group so we can reap cbmc grandchildren
            )
            try:
                _out, _err = _popen.communicate(timeout=timeout)
                proc = _sub.CompletedProcess(_popen.args, _popen.returncode, _out, _err)
            except _sub.TimeoutExpired:
                # cargo kani spawns cbmc grandchildren. subprocess timeout kills only
                # the direct child, orphaning cbmc (which then pegs a core for hours).
                # Kill the whole process group instead.
                try:
                    _os.killpg(_os.getpgid(_popen.pid), _signal.SIGKILL)
                except Exception:
                    pass
                try:
                    _popen.wait(timeout=10)
                except Exception:
                    pass
                if _os.environ.get("BMC_AGENT_DEBUG_CARGO"):
                    try:
                        snap = Path(f"/tmp/bmc_debug_{harness_name}_timeout.rs")
                        snap.write_bytes(appended)
                    except Exception:
                        pass
                return CBMCResult(verified=False, error=f"cargo kani timed out after {timeout}s")
        except FileNotFoundError:
            return CBMCResult(verified=False, error="cargo or kani not found")
        except OSError as exc:
            return CBMCResult(verified=False, error=f"cargo kani OS error: {exc}")
        raw = proc.stdout or ""
        stderr = proc.stderr or ""
        # Debug dump: snapshot the appended source + raw outputs when the env
        # flag is set. Helps diagnose 'harness not discovered' regressions.
        import os as _os
        if _os.environ.get("BMC_AGENT_DEBUG_CARGO"):
            try:
                base = Path(f"/tmp/bmc_debug_{harness_name}")
                base.with_suffix(".src.rs").write_bytes(appended)
                base.with_suffix(".stdout").write_text(raw)
                base.with_suffix(".stderr").write_text(stderr)
            except Exception:
                pass
        result = _parse_kani_output(raw, stderr, proc.returncode)
        # When cargo-mode fails to even compile the crate, _parse_kani_output
        # only sees "could not compile due to N errors" on stdout. The real
        # rustc E0XXX errors are on stderr. Surface them in the error field
        # so downstream debug knows it's a crate-incompatible-with-kani
        # situation, not a harness-generation bug.
        if not result.verified and not result.counterexamples:
            # Include stdout tail too -- cargo kani's actual error messages
            # like 'no harnesses matched harness filter' live on stdout, not
            # stderr. Without this we can't tell whether the failure was
            # rustc-compile or harness-discovery.
            err_combined = (
                (result.error or "")
                + "\n--- stdout (tail) ---\n" + raw[-2000:]
                + "\n--- stderr (tail) ---\n" + stderr[-2000:]
            )
            result.error = err_combined.strip()[:8000]
        return result
    finally:
        # Restore the source file BEFORE releasing the crate lock, otherwise
        # another worker thread can acquire the lock and read polluted bytes
        # (with this thread's appended harness still in place). Concretely
        # we observed cargo kani saying 'no harnesses matched' on
        # check_from_checksum while the file already had check_adler32_slice
        # from a parallel thread -- multiple appends accumulating.
        if use_git:
            try:
                _sub2.run(
                    ["git", "checkout", "--", "."], cwd=str(crate_root),
                    capture_output=True, timeout=30,
                )
            except Exception:
                # Last-resort: rewrite the one file we modified.
                try:
                    source_path.write_bytes(original_bytes)
                except OSError:
                    pass
        else:
            try:
                source_path.write_bytes(original_bytes)
            except OSError:
                import logging as _logging
                _logging.getLogger("bmc_agent.kani").error(
                    "Failed to restore %s after cargo-kani run", source_path,
                )
        _release_crate_lock(crate_lock)


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
    # cargo-mode "no harnesses matched" diagnostic: cargo kani built the
    # crate but the --harness filter didn't find the proof (could be the
    # harness function has a type error and was skipped, or the cfg(kani)
    # didn't expose it). Surface as a distinguishable error so callers
    # don't misclassify as compile-fail or transient.
    if "no harnesses matched" in raw or "No proof harnesses" in raw:
        return CBMCResult(
            verified=False, raw_output=raw,
            error="cargo-kani: harness not discovered (check harness body for type errors or cfg gating)",
        )

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

    # Vacuous-proof guard: if Kani says SUCCESSFUL but EVERY check is
    # UNREACHABLE (no SUCCESS rows), the harness's preconditions pruned
    # the entire state space (e.g. `kani::assume(false)` -- typical LLM
    # giveup pattern). Such "verified" verdicts are meaningless, since
    # nothing was actually checked. Mark as failed with a clear error so
    # the spec generator / classifier can refine instead of recording a
    # bogus clean.
    #
    # Detection: the summary line has the form
    #   ** 0 of N failed (N unreachable)
    # when the entire harness is vacuous. We also check individual Check
    # rows: if there's at least one row and all are UNREACHABLE, treat as
    # vacuous. Combine both signals.
    if has_success:
        # Quick scan for "(... unreachable)" in the summary
        summary_match = re.search(r"\*\*\s*0\s+of\s+(\d+)\s+failed\s*\((\d+)\s+unreachable\)", raw)
        if summary_match and summary_match.group(1) == summary_match.group(2) and int(summary_match.group(1)) > 0:
            return CBMCResult(
                verified=False, counterexamples=[], raw_output=raw,
                error="vacuous proof: all properties unreachable (likely kani::assume(false) from a precondition that prunes every state)",
            )
        # Per-check scan: count SUCCESS vs UNREACHABLE
        success_count = len(re.findall(r"-\s*Status:\s*SUCCESS\b", raw))
        unreachable_count = len(re.findall(r"-\s*Status:\s*UNREACHABLE\b", raw))
        if success_count == 0 and unreachable_count > 0:
            return CBMCResult(
                verified=False, counterexamples=[], raw_output=raw,
                error="vacuous proof: all properties unreachable (likely kani::assume(false) from a precondition that prunes every state)",
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
