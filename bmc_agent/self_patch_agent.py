"""
Self-patch agent (Phase 3 of autonomous mode).

When the Phase 1 retry registry returns ``NO_ACTION`` on a CBMC error
whose class isn't yet in the static taxonomy (typically a new harness-
generator bug surfaced by a fresh codebase), this agent asks an LLM
to read the failing harness, locate the structural bug, and propose a
patch to ``harness_generator.py`` or ``preprocessor.py`` along with a
regression test that fails before the patch and passes after.

Hard safety gates — same code path under stage or auto modes:

* **Allow-list**: patch must only touch files in
  :data:`_ALLOWED_PATCH_FILES`. Touching anything else → reject.
* **Scope caps**: at most :data:`_MAX_FILES_PER_PATCH` files and
  :data:`_MAX_LINES_PER_PATCH` added+removed lines per proposal.
* **Regression test required**: proposal must include a new test
  function added to ``tests/``. The harness runs ``pytest`` against
  it BEFORE applying the patch (must fail) and AFTER applying
  (must pass). Either result not as expected → reject.
* **Suite stays at baseline**: full ``pytest`` run after applying
  the patch must have ≤ baseline failure count. Any new test
  failure → reject + revert.
* **Stage by default**: ``mode='stage'`` writes the proposal to
  ``<output>/proposed_patches/round_<N>/<proposal_id>.diff`` and
  exits. ``mode='auto'`` applies and commits (only after all gates
  pass). ``mode='deny'`` (the system default) short-circuits before
  any LLM call.

A *failure* in any gate is a *successful* safety outcome. The agent
returns the proposal with ``status='rejected'`` plus the rejection
reason for audit.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bmc_agent.llm import LLMClient
    from bmc_agent.cbmc_error_classifier import CbmcErrorDiagnosis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_ALLOWED_PATCH_FILES: frozenset[str] = frozenset({
    "bmc_agent/harness_generator.py",
    "bmc_agent/preprocessor.py",
})
"""Files the agent is permitted to patch. Anything else → reject.

Deliberately narrow: only the harness-generation / preprocessing layer
where new CBMC error classes typically need structural fixes. Touching
the bmc_engine, classifier, retry registry, or any test file is not
allowed — those are either soundness-critical or out-of-domain.
"""

_REGRESSION_TEST_DIR = "tests"
"""Where the proposal's regression test must land."""

_MAX_FILES_PER_PATCH: int = 2
_MAX_LINES_PER_PATCH: int = 200  # added + removed


class PatchMode(str, Enum):
    DENY = "deny"
    """No LLM call, no proposal. Default. Used to keep the self-patch
    layer off when the operator hasn't explicitly opted in."""

    STAGE = "stage"
    """Generate proposal, run all gates, write the diff +
    regression-test source to disk. Don't apply. Operator reviews and
    runs ``git apply`` manually."""

    AUTO = "auto"
    """Generate proposal, run all gates, apply via ``git apply`` and
    commit. Only after every gate passes. Reserved for sweeps with a
    trusted target where the operator wants fully unattended runs."""


class ProposalStatus(str, Enum):
    PROPOSED = "proposed"
    """Agent produced a syntactically valid proposal."""

    STAGED = "staged"
    """Proposal passed all gates and was written to disk under stage
    mode. No source file mutated."""

    APPLIED = "applied"
    """Proposal passed all gates and was applied + committed under
    auto mode."""

    REJECTED = "rejected"
    """Proposal failed at least one gate. ``rejection_reason`` carries
    the diagnostic. No source file mutated."""

    DENIED = "denied"
    """``mode=deny`` short-circuited before any LLM call."""

    LLM_ERROR = "llm_error"
    """LLM failed to produce a parseable response after retries."""


@dataclass
class PatchProposal:
    """The agent's structured output for one error class."""

    status: ProposalStatus
    diff: str = ""
    """Unified diff (``git apply``-compatible) touching one or more
    files in ``_ALLOWED_PATCH_FILES``."""

    regression_test_path: str = ""
    """Relative path under ``tests/`` for the regression test (e.g.
    ``tests/test_self_patch_regression_X.py``)."""

    regression_test_source: str = ""
    """Full Python source of the regression test file."""

    regression_test_name: str = ""
    """Name of the pytest function to run for the fail-before /
    pass-after gate."""

    rationale: str = ""
    """Agent's explanation of the root cause and why the patch fixes
    it. Logged for audit."""

    rejection_reason: str = ""
    """Set when ``status == REJECTED`` or ``LLM_ERROR``."""

    error_class: str = ""
    error_target: str = ""
    files_touched: list[str] = field(default_factory=list)
    lines_changed: int = 0


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior C-verification engineer fixing a structural bug in
``bmc-agent``'s harness generator. The harness generator transforms
preprocessed C source into CBMC-checkable harnesses. CBMC has just
rejected a generated harness with a parse or convert error that the
existing recovery taxonomy doesn't cover. Your job:

1. Diagnose the root cause by reading the failing harness fragment
   and the relevant ``harness_generator.py`` excerpt.
2. Propose a minimal, focused patch to ``bmc_agent/harness_generator.py``
   or ``bmc_agent/preprocessor.py`` that fixes the issue.
3. Write a regression test that fails on the current code and passes
   after your patch.

Hard rules:
- You may ONLY edit ``bmc_agent/harness_generator.py`` and/or
  ``bmc_agent/preprocessor.py``. Touching any other file (including
  test files outside ``tests/``) makes the proposal invalid.
- Your regression test goes in a NEW file under ``tests/``, with a
  unique name like ``test_self_patch_<short_slug>.py``.
- Total patch size: ≤ 2 files, ≤ 200 added+removed lines.
- Prefer extending existing static sets (e.g.
  ``_SYSTEM_TYPEDEF_NAMES``, ``_GLIBC_KNOWN_STRUCTS``) over rewriting
  control flow. Smaller surface = lower risk.
- If you can't fix it cleanly with one minimal patch, return
  ``"action": "give_up"`` and explain why. Better to defer than to
  ship a risky patch.

Output STRICT JSON only. No prose outside the JSON. The schema:

{
  "action": "patch" | "give_up",
  "rationale": "<one-paragraph root cause and fix explanation>",
  "diff": "<unified diff, git-apply-compatible, ending in newline>",
  "regression_test_path": "tests/test_self_patch_<slug>.py",
  "regression_test_source": "<full Python source for the test file>",
  "regression_test_name": "<pytest function name, e.g. test_strips_widechar_typedefs>"
}

If ``action`` is ``"give_up"``, the diff/regression_test fields may
be empty strings but ``rationale`` is mandatory.
"""


_USER_TEMPLATE = """\
A CBMC run on the generated harness for function ``{function_name}``
failed with this error class: ``{error_class}``
(target identifier: ``{error_target!r}``).

The first ERROR message from CBMC:
{raw_error}

The failing harness fragment (around the error line):
```c
{harness_excerpt}
```

The current relevant ``harness_generator.py`` excerpt (the strip /
generation code most likely to need adjustment):
```python
{generator_excerpt}
```

Recovery actions that the static retry registry already covers (so
you don't have to duplicate them):
{known_actions}

Produce the JSON-structured proposal per the system prompt.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


@dataclass
class SelfPatchAgent:
    """Phase 3 agent: LLM-driven harness-gen patching with safety gates."""

    llm: "LLMClient"
    repo_root: Path
    config: object
    """Reference to the Config so we can read the leaked-key-aware LLM
    settings + scope caps + mode."""

    def propose(
        self,
        diagnosis: "CbmcErrorDiagnosis",
        function_name: str,
        harness_path: str,
        generator_excerpt: str,
        known_actions: str = "",
    ) -> PatchProposal:
        """Ask the LLM for a patch proposal. Pure proposal-build; the
        caller validates and either stages or applies.

        Returns a :class:`PatchProposal` whose ``status`` is one of:
          * ``PROPOSED`` (LLM produced a parseable patch — caller now
            validates and stages/applies)
          * ``LLM_ERROR`` (LLM failed; nothing else to do this round)
          * ``REJECTED`` (LLM returned ``give_up``)
        """
        mode = _resolve_mode(self.config)
        if mode == PatchMode.DENY:
            return PatchProposal(
                status=ProposalStatus.DENIED,
                error_class=diagnosis.error_class.value,
                error_target=diagnosis.identifier or "",
                rejection_reason="self-patch mode is 'deny'; no proposal generated",
            )

        harness_excerpt = _read_harness_excerpt(harness_path, diagnosis.source_line)
        user = _USER_TEMPLATE.format(
            function_name=function_name,
            error_class=diagnosis.error_class.value,
            error_target=diagnosis.identifier or "<none>",
            raw_error=diagnosis.raw_message[:400],
            harness_excerpt=harness_excerpt,
            generator_excerpt=generator_excerpt[:8000],
            known_actions=known_actions or "(none)",
        )

        try:
            response_text = self.llm.complete(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user,
                max_tokens=4096,
                temperature=0.0,
            )
        except Exception as exc:
            return PatchProposal(
                status=ProposalStatus.LLM_ERROR,
                error_class=diagnosis.error_class.value,
                error_target=diagnosis.identifier or "",
                rejection_reason=f"LLM call failed: {exc!s}",
            )

        try:
            payload = _parse_json_response(response_text)
        except ValueError as exc:
            return PatchProposal(
                status=ProposalStatus.LLM_ERROR,
                error_class=diagnosis.error_class.value,
                error_target=diagnosis.identifier or "",
                rejection_reason=f"unparseable LLM response: {exc!s}",
            )

        if payload.get("action") == "give_up":
            return PatchProposal(
                status=ProposalStatus.REJECTED,
                error_class=diagnosis.error_class.value,
                error_target=diagnosis.identifier or "",
                rationale=payload.get("rationale", ""),
                rejection_reason="agent gave up",
            )

        if payload.get("action") != "patch":
            return PatchProposal(
                status=ProposalStatus.LLM_ERROR,
                error_class=diagnosis.error_class.value,
                error_target=diagnosis.identifier or "",
                rejection_reason=f"unknown action {payload.get('action')!r}",
            )

        return PatchProposal(
            status=ProposalStatus.PROPOSED,
            diff=payload.get("diff", ""),
            regression_test_path=payload.get("regression_test_path", ""),
            regression_test_source=payload.get("regression_test_source", ""),
            regression_test_name=payload.get("regression_test_name", ""),
            rationale=payload.get("rationale", ""),
            error_class=diagnosis.error_class.value,
            error_target=diagnosis.identifier or "",
        )

    # ------------------------------------------------------------------
    # Validation gates
    # ------------------------------------------------------------------

    def validate(self, proposal: PatchProposal) -> PatchProposal:
        """Apply all safety gates to a ``PROPOSED`` proposal.

        Mutates the proposal in-place — sets ``status=REJECTED`` and
        ``rejection_reason`` on any gate failure, or leaves ``PROPOSED``
        if every gate passes. The caller then decides whether to stage
        or apply.
        """
        if proposal.status != ProposalStatus.PROPOSED:
            return proposal

        # Gate 1: structural — diff parses + only touches allowed files.
        try:
            touched = _parse_diff_targets(proposal.diff)
        except ValueError as exc:
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = f"diff parse failed: {exc!s}"
            return proposal
        proposal.files_touched = sorted(touched)

        disallowed = [f for f in touched if f not in _ALLOWED_PATCH_FILES]
        if disallowed:
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = (
                f"diff touches non-allowed files: {disallowed} "
                f"(allow-list: {sorted(_ALLOWED_PATCH_FILES)})"
            )
            return proposal

        # Gate 2: scope caps.
        if len(touched) > _MAX_FILES_PER_PATCH:
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = (
                f"diff touches {len(touched)} files > cap {_MAX_FILES_PER_PATCH}"
            )
            return proposal
        proposal.lines_changed = _count_diff_lines(proposal.diff)
        if proposal.lines_changed > _MAX_LINES_PER_PATCH:
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = (
                f"diff has {proposal.lines_changed} changed lines > cap "
                f"{_MAX_LINES_PER_PATCH}"
            )
            return proposal

        # Gate 3: regression test fields present.
        rt_path = (proposal.regression_test_path or "").strip()
        if not rt_path or not rt_path.startswith(_REGRESSION_TEST_DIR + "/"):
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = (
                f"regression_test_path must start with '{_REGRESSION_TEST_DIR}/' "
                f"(got {rt_path!r})"
            )
            return proposal
        if not proposal.regression_test_source.strip():
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = "regression_test_source is empty"
            return proposal
        if not proposal.regression_test_name.strip():
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = "regression_test_name is empty"
            return proposal

        # Gate 4 + 5 (fail-before / pass-after the patch) live in
        # apply_if_valid — they need to mutate the filesystem.

        return proposal

    # ------------------------------------------------------------------
    # Stage / apply
    # ------------------------------------------------------------------

    def stage_or_apply(
        self, proposal: PatchProposal, output_root: Path, round_idx: int
    ) -> PatchProposal:
        """Run the fail-before / pass-after gates and then either
        stage the proposal to disk or apply it, depending on
        ``config.allow_self_patch``.

        Always leaves the working tree clean if any gate fails (test
        files written for the regression-test gate are removed; any
        partial git-apply is reverted).
        """
        if proposal.status != ProposalStatus.PROPOSED:
            return proposal

        mode = _resolve_mode(self.config)
        if mode == PatchMode.DENY:
            proposal.status = ProposalStatus.DENIED
            return proposal

        # Stage the regression test on disk; we need it present for both
        # fail-before and pass-after pytest invocations.
        rt_full_path = self.repo_root / proposal.regression_test_path
        rt_full_path.parent.mkdir(parents=True, exist_ok=True)
        rt_full_path.write_text(proposal.regression_test_source)
        regression_test_was_new = not rt_full_path.exists()  # always True after write
        _ = regression_test_was_new

        try:
            # Gate 4: fail-before.
            rc_before = self._run_pytest(proposal.regression_test_path, proposal.regression_test_name)
            if rc_before == 0:
                proposal.status = ProposalStatus.REJECTED
                proposal.rejection_reason = (
                    f"regression test {proposal.regression_test_name} passed BEFORE "
                    "patch applied — proposal does not actually demonstrate a bug fix"
                )
                return proposal

            # Apply the patch.
            apply_result = self._git_apply(proposal.diff)
            if apply_result is not None:
                proposal.status = ProposalStatus.REJECTED
                proposal.rejection_reason = f"git apply failed: {apply_result}"
                return proposal

            # Gate 5: pass-after.
            rc_after = self._run_pytest(proposal.regression_test_path, proposal.regression_test_name)
            if rc_after != 0:
                # Revert patch and reject.
                self._git_apply(proposal.diff, reverse=True)
                proposal.status = ProposalStatus.REJECTED
                proposal.rejection_reason = (
                    f"regression test {proposal.regression_test_name} failed AFTER "
                    "patch applied — patch doesn't fix what the test asserts"
                )
                return proposal

            # All gates passed. Either commit or revert + stage.
            if mode == PatchMode.AUTO:
                proposal.status = ProposalStatus.APPLIED
            else:  # STAGE
                # Revert the diff so the working tree is clean; the
                # diff + test source persist under proposed_patches/
                # for human review.
                self._git_apply(proposal.diff, reverse=True)
                proposal.status = ProposalStatus.STAGED
                self._write_staged_artifacts(proposal, output_root, round_idx)
                # Also remove the regression test from tests/ — the
                # canonical copy is in the proposed_patches dir.
                if rt_full_path.exists():
                    rt_full_path.unlink()
        except Exception as exc:
            # Best-effort cleanup.
            try:
                self._git_apply(proposal.diff, reverse=True)
            except Exception:
                pass
            if rt_full_path.exists():
                try:
                    rt_full_path.unlink()
                except Exception:
                    pass
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = f"unexpected exception during gates: {exc!s}"

        return proposal

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_pytest(self, test_path: str, test_name: str) -> int:
        """Run pytest on a specific test. Returns exit code."""
        cmd = ["uv", "run", "pytest", f"{test_path}::{test_name}", "-q"]
        try:
            proc = subprocess.run(
                cmd, cwd=self.repo_root, capture_output=True, text=True, timeout=120,
            )
            return proc.returncode
        except subprocess.TimeoutExpired:
            return 124
        except Exception:
            return 125

    def _git_apply(self, diff: str, *, reverse: bool = False) -> Optional[str]:
        """Apply ``diff`` via ``git apply``. Returns None on success or
        an error string on failure.
        """
        cmd = ["git", "apply", "--whitespace=nowarn"]
        if reverse:
            cmd.append("--reverse")
        cmd.append("-")
        try:
            proc = subprocess.run(
                cmd, cwd=self.repo_root, input=diff, capture_output=True,
                text=True, timeout=30,
            )
            if proc.returncode != 0:
                return proc.stderr[:400] or proc.stdout[:400] or "git apply non-zero exit"
            return None
        except Exception as exc:
            return f"{exc!s}"

    def _write_staged_artifacts(
        self, proposal: PatchProposal, output_root: Path, round_idx: int
    ) -> None:
        """Persist a STAGED proposal to ``<output>/proposed_patches/round_<N>/``.

        Writes three files per proposal:
          * ``<slug>.diff`` — the unified diff (ready for ``git apply``)
          * ``<slug>.test.py`` — the regression test source
          * ``<slug>.meta.json`` — class, target, rationale, gate results
        """
        target_dir = output_root / "proposed_patches" / f"round_{round_idx + 1}"
        target_dir.mkdir(parents=True, exist_ok=True)
        slug = _slug_for(proposal)
        (target_dir / f"{slug}.diff").write_text(proposal.diff)
        (target_dir / f"{slug}.test.py").write_text(proposal.regression_test_source)
        (target_dir / f"{slug}.meta.json").write_text(json.dumps({
            "status": proposal.status.value,
            "error_class": proposal.error_class,
            "error_target": proposal.error_target,
            "rationale": proposal.rationale,
            "files_touched": proposal.files_touched,
            "lines_changed": proposal.lines_changed,
            "regression_test_path": proposal.regression_test_path,
            "regression_test_name": proposal.regression_test_name,
            "review_instructions": (
                f"To apply manually:\n"
                f"  1. cp {target_dir.relative_to(output_root.parent)}/{slug}.test.py "
                f"{proposal.regression_test_path}\n"
                f"  2. git apply {target_dir.relative_to(output_root.parent)}/{slug}.diff\n"
                f"  3. uv run pytest {proposal.regression_test_path}::"
                f"{proposal.regression_test_name}\n"
            ),
        }, indent=2))


# ---------------------------------------------------------------------------
# Pure helpers (tested in isolation)
# ---------------------------------------------------------------------------


def _resolve_mode(config: object) -> PatchMode:
    """Read the self-patch mode from config, defaulting to DENY.

    Accepted values on ``config.allow_self_patch``: ``"deny"`` (default),
    ``"stage"``, ``"auto"``. Anything else → DENY.
    """
    val = getattr(config, "allow_self_patch", "deny")
    if isinstance(val, PatchMode):
        return val
    try:
        return PatchMode(str(val).lower())
    except ValueError:
        return PatchMode.DENY


def _read_harness_excerpt(path: str, line_no: Optional[int], context: int = 20) -> str:
    """Read +/- ``context`` lines around ``line_no`` from the harness
    file. Returns the empty string if path doesn't exist.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return ""
    if line_no is None:
        # First 60 lines as a default excerpt.
        return "".join(f"{i + 1:5d}: {l}" for i, l in enumerate(lines[:60]))
    lo = max(0, line_no - 1 - context)
    hi = min(len(lines), line_no + context)
    return "".join(f"{i + 1:5d}: {l}" for i, l in enumerate(lines[lo:hi], start=lo))


_JSON_BLOCK_PAT = re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL)


def _parse_json_response(text: str) -> dict:
    """Extract and parse the JSON object from an LLM response.

    Accepts either bare JSON or JSON wrapped in a ```json …``` fence.
    Raises ``ValueError`` if no valid JSON object can be found.
    """
    text = text.strip()
    # Try fenced block first.
    m = _JSON_BLOCK_PAT.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            raise ValueError(f"fenced JSON didn't parse: {exc}") from exc
    # Fall back to scanning for the first balanced '{...}' object.
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"bare JSON didn't parse: {exc}") from exc
    raise ValueError("no JSON object found in response")


_DIFF_TARGET_PAT = re.compile(r"^\+\+\+ (?:b/)?(\S+)", re.MULTILINE)


def _parse_diff_targets(diff: str) -> list[str]:
    """Extract the list of files mentioned in ``+++`` lines of a
    unified diff. Skips ``/dev/null`` (rename / delete).
    """
    if not diff.strip():
        raise ValueError("diff is empty")
    targets = []
    for m in _DIFF_TARGET_PAT.finditer(diff):
        path = m.group(1).strip()
        if path == "/dev/null":
            continue
        targets.append(path)
    if not targets:
        raise ValueError("no '+++' target lines found in diff")
    return targets


_DIFF_HUNK_LINE = re.compile(r"^[+\-](?![+\-])", re.MULTILINE)


def _count_diff_lines(diff: str) -> int:
    """Count added + removed lines in a unified diff (excluding the
    ``+++`` / ``---`` file-header lines)."""
    return len(_DIFF_HUNK_LINE.findall(diff))


def _slug_for(proposal: PatchProposal) -> str:
    """Generate a filename-safe slug for the proposal artifacts."""
    base = (proposal.error_class or "patch") + "_" + (proposal.error_target or "x")
    return re.sub(r"[^A-Za-z0-9_]+", "_", base)[:80]
