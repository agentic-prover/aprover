"""
Corpus management for GRACE evaluation.

Manages a collection of C programs (corpus entries) used for evaluation.
Each entry has a source file, optional ground-truth bug annotations, and metadata.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amc.llm import LLMClient


@dataclass
class GroundTruthBug:
    """A manually-annotated known bug in a corpus entry."""

    function_name: str
    bug_type: str
    description: str
    line_number: int | None = None


@dataclass
class CorpusEntry:
    """A single entry in the evaluation corpus."""

    name: str
    source_file: str
    ground_truth_bugs: list[GroundTruthBug] = field(default_factory=list)
    driver_type: str = "unknown"       # "ring_buffer", "block_device", "char_device", "network", etc.
    generated_by: str = "manual"       # "claude", "codex", "manual", etc.


class Corpus:
    """
    Manages a collection of C programs for evaluation.

    Directory layout::

        {corpus_dir}/
            {entry_name}/
                source.c          # the C source file
                metadata.json     # name, driver_type, generated_by
                ground_truth.json # list of known bugs (optional)

    When loading from a flat directory (e.g. examples/), each .c file is
    treated as a corpus entry with no ground truth unless a matching
    metadata.json or ground_truth.json exists.
    """

    def __init__(self, corpus_dir: str) -> None:
        self.corpus_dir = Path(corpus_dir)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> list[CorpusEntry]:
        """
        Load all corpus entries from corpus_dir.

        Supports two layouts:
        1. Subdirectory layout: each subdir contains source.c + optional metadata/ground_truth.
        2. Flat layout: each .c file in corpus_dir is treated as a corpus entry.
        """
        entries: list[CorpusEntry] = []

        if not self.corpus_dir.exists():
            return entries

        # Check for subdirectory layout first
        subdirs = [p for p in self.corpus_dir.iterdir() if p.is_dir()]
        c_files_in_root = list(self.corpus_dir.glob("*.c"))

        if subdirs:
            # Try subdirectory layout
            for subdir in sorted(subdirs):
                entry = self._load_from_subdir(subdir)
                if entry is not None:
                    entries.append(entry)

        # Also load bare .c files in the root directory
        for c_file in sorted(c_files_in_root):
            entry = self._load_from_c_file(c_file)
            if entry is not None:
                entries.append(entry)

        return entries

    def _load_from_subdir(self, subdir: Path) -> CorpusEntry | None:
        """Load a corpus entry from a subdirectory."""
        source_c = subdir / "source.c"
        if not source_c.exists():
            return None

        metadata = self._read_metadata(subdir / "metadata.json")
        ground_truth = self._read_ground_truth(subdir / "ground_truth.json")

        return CorpusEntry(
            name=metadata.get("name", subdir.name),
            source_file=str(source_c),
            ground_truth_bugs=ground_truth,
            driver_type=metadata.get("driver_type", "unknown"),
            generated_by=metadata.get("generated_by", "manual"),
        )

    def _load_from_c_file(self, c_file: Path) -> CorpusEntry | None:
        """Load a corpus entry from a bare .c file (flat layout)."""
        stem = c_file.stem
        # Look for optional sidecar files in the same directory
        metadata_path = c_file.parent / f"{stem}_metadata.json"
        ground_truth_path = c_file.parent / f"{stem}_ground_truth.json"

        metadata = self._read_metadata(metadata_path)
        ground_truth = self._read_ground_truth(ground_truth_path)

        return CorpusEntry(
            name=metadata.get("name", stem),
            source_file=str(c_file),
            ground_truth_bugs=ground_truth,
            driver_type=metadata.get("driver_type", "unknown"),
            generated_by=metadata.get("generated_by", "manual"),
        )

    def _read_metadata(self, path: Path) -> dict:
        """Read a metadata.json file, returning an empty dict if absent."""
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def _read_ground_truth(self, path: Path) -> list[GroundTruthBug]:
        """Read a ground_truth.json file, returning an empty list if absent."""
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            bugs: list[GroundTruthBug] = []
            for item in data:
                if isinstance(item, dict):
                    bugs.append(
                        GroundTruthBug(
                            function_name=item.get("function_name", ""),
                            bug_type=item.get("bug_type", "unknown"),
                            description=item.get("description", ""),
                            line_number=item.get("line_number"),
                        )
                    )
            return bugs
        except (json.JSONDecodeError, OSError):
            return []

    # ------------------------------------------------------------------
    # Adding entries
    # ------------------------------------------------------------------

    def add_entry(self, entry: CorpusEntry) -> None:
        """
        Add a new corpus entry to the corpus directory.

        Creates:
          {corpus_dir}/{entry.name}/source.c
          {corpus_dir}/{entry.name}/metadata.json
          {corpus_dir}/{entry.name}/ground_truth.json  (if bugs are present)
        """
        entry_dir = self.corpus_dir / entry.name
        entry_dir.mkdir(parents=True, exist_ok=True)

        # Copy or create source.c
        dest_source = entry_dir / "source.c"
        src = Path(entry.source_file)
        if src.exists() and src != dest_source:
            shutil.copy2(src, dest_source)
        elif not dest_source.exists() and src.exists():
            shutil.copy2(src, dest_source)

        # Write metadata.json
        metadata = {
            "name": entry.name,
            "driver_type": entry.driver_type,
            "generated_by": entry.generated_by,
        }
        with (entry_dir / "metadata.json").open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2)

        # Write ground_truth.json (always write, even if empty)
        gt_data = [
            {
                "function_name": bug.function_name,
                "bug_type": bug.bug_type,
                "description": bug.description,
                "line_number": bug.line_number,
            }
            for bug in entry.ground_truth_bugs
        ]
        with (entry_dir / "ground_truth.json").open("w", encoding="utf-8") as fh:
            json.dump(gt_data, fh, indent=2)

    # ------------------------------------------------------------------
    # Synthetic corpus generation
    # ------------------------------------------------------------------

    def generate_synthetic_corpus(
        self,
        output_dir: str,
        llm: "LLMClient",
        count: int = 5,
    ) -> list[CorpusEntry]:
        """
        Use an LLM to generate ``count`` simple C programs with intentional bugs.

        For each program:
        1. Prompt LLM to write a small C driver-like program (~100-200 lines).
        2. Ask it to include 1-3 intentional bugs and document them.
        3. Save as a corpus entry in ``output_dir``.

        Returns the list of generated CorpusEntry objects.

        Note: In tests this method should be mocked — it makes real LLM calls.
        """
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        driver_types = [
            "ring_buffer",
            "char_device",
            "block_device",
            "memory_allocator",
            "network_queue",
        ]

        entries: list[CorpusEntry] = []
        for i in range(count):
            dtype = driver_types[i % len(driver_types)]
            entry = self._generate_one_program(llm, out_path, dtype, index=i)
            if entry is not None:
                entries.append(entry)

        return entries

    def _generate_one_program(
        self,
        llm: "LLMClient",
        output_dir: Path,
        driver_type: str,
        index: int,
    ) -> CorpusEntry | None:
        """Generate a single synthetic C program using the LLM."""
        system_prompt = (
            "You are an expert C programmer who writes small, self-contained "
            "driver-like programs for formal verification research."
        )
        user_prompt = (
            f"Write a small self-contained C program implementing a {driver_type} "
            f"(approximately 100-200 lines). Include 1-3 intentional bugs "
            f"(e.g., off-by-one errors, missing null checks, integer overflows). "
            f"Document each bug with a comment starting with '/* BUG:'. "
            f"Return JSON with fields: "
            f"\"source_code\" (the full C code as a string), "
            f"\"bugs\" (list of objects with function_name, bug_type, description, line_number), "
            f"\"driver_type\" (string)."
        )

        try:
            response = llm.complete(system_prompt, user_prompt)
            data = _parse_json_response(response)
            if data is None:
                return None

            source_code: str = data.get("source_code", "")
            if not source_code.strip():
                return None

            bugs_raw: list[dict] = data.get("bugs", [])
            dtype_out: str = data.get("driver_type", driver_type)
            entry_name = f"synthetic_{dtype_out}_{index:02d}"

            entry_dir = output_dir / entry_name
            entry_dir.mkdir(parents=True, exist_ok=True)
            (entry_dir / "source.c").write_text(source_code, encoding="utf-8")

            ground_truth_bugs = [
                GroundTruthBug(
                    function_name=b.get("function_name", ""),
                    bug_type=b.get("bug_type", "unknown"),
                    description=b.get("description", ""),
                    line_number=b.get("line_number"),
                )
                for b in bugs_raw
                if isinstance(b, dict)
            ]

            entry = CorpusEntry(
                name=entry_name,
                source_file=str(entry_dir / "source.c"),
                ground_truth_bugs=ground_truth_bugs,
                driver_type=dtype_out,
                generated_by="claude",
            )
            self.add_entry(entry)
            return entry

        except Exception:
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json_response(text: str) -> dict | None:
    """Parse a JSON object from LLM output, stripping markdown fences."""
    import re

    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner: list[str] = []
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                inner.append(line)
        text = "\n".join(inner).strip()

    try:
        import json as _json
        data = _json.loads(text)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            import json as _json
            data = _json.loads(match.group())
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass

    return None
