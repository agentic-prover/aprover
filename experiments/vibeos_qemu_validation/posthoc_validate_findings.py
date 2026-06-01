#!/usr/bin/env python3
"""Post-hoc VibeOS QEMU validation for existing BMC-Agent findings.

This runner does not rediscover bugs. It reads already-produced Markdown
findings, maps supported functions to the current VibeOS QEMU replay catalog,
and records target-side validation evidence for each finding.
"""

from __future__ import annotations

import argparse
from collections import Counter
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.vibeos_qemu_dynamic_replay import (  # noqa: E402
    CASE_BY_ENTRY,
    REPLAY_CATALOG,
    _copy_vibeos_repo,
    _ensure_tlse_submodule,
    _first_marker_line,
    _guard_replay_injection,
    _has_target_fault,
    _invalid_confirmed_marker_reason,
    _invalid_generated_call_reason,
    _invalid_unquoted_marker_reason,
    _prepare_qemu_disk_image,
    _qemu_command,
    _run,
    _tail,
    _vibeos_build_command,
)


HEADER_RE = re.compile(
    r"^#\s*(?P<bug_id>BUG-\d+)\s+.*?`(?P<function>[^`]+)`(?:\s*\((?P<short_module>[^)]+)\))?",
    re.MULTILINE,
)
FIELD_RE = re.compile(r"^\|\s*\*\*(?P<key>[^*]+)\*\*\s*\|\s*(?P<value>.*?)\s*\|$", re.MULTILINE)
PROPERTY_RE = re.compile(r"\*\*Violated property:\*\*\s*`(?P<property>[^`]+)`")
TARGET_EVENT_RE = re.compile(r"\btarget_event=(?P<event>[A-Za-z0-9_:-]+)")


GENERATED_ANCHORS = {
    "after_hal_dma_init": "    hal_dma_init();\n",
    "after_net_init": "    net_init();\n",
    "after_vfs_init": "    vfs_init();\n",
    "after_kapi_init_log": '    printf("[KERNEL] Kernel API initialized\\n");\n',
    "after_process_init": "    process_init();\n",
}

BANNED_INJECTION_PATTERNS = (
    "#include",
    "system(",
    "popen(",
    "fork(",
    "execve(",
    "execl(",
    "execv(",
    "extern ",
    "while (1)",
    "while(1)",
    "for (;;)",
    "for(;;)",
)


EXPECTED_OUTCOME_BY_CASE = {
    "hal_dma_fb_copy_overflow": "qemu_confirmed",
    "net_get_mac_null": "observed_safety_concern",
    "kapi_file_size_invalid_ptr": "observed_safety_concern",
    "kapi_delete_invalid_path": "qemu_confirmed",
    "kapi_rename_invalid_path": "qemu_confirmed",
    "kapi_get_datetime_invalid_ptr": "qemu_confirmed",
    "mouse_set_pos_large_coordinate": "observed_safety_concern",
    "malloc_size_wrap": "qemu_confirmed",
    "vfs_read_null_file_data": "observed_safety_concern",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    manifest_p = sub.add_parser("manifest", help="Build a JSONL manifest from finding markdown files")
    add_common_args(manifest_p)
    manifest_p.add_argument("--output", required=True, help="Output JSONL path")

    run_p = sub.add_parser("run", help="Run QEMU replay for supported findings")
    add_common_args(run_p)
    run_p.add_argument("--repo", required=True, help="Path to VibeOS repository")
    run_p.add_argument("--output", required=True, help="Output artifact directory")
    run_p.add_argument("--build-timeout", type=int, default=180)
    run_p.add_argument("--qemu-timeout", type=int, default=20)
    run_p.add_argument(
        "--with-fat32-disk",
        action="store_true",
        help="Attach a generated FAT32 virtio-blk disk with a TTF font resource",
    )
    run_p.add_argument(
        "--font-file",
        default=os.environ.get("BMC_AGENT_VIBEOS_FONT_FILE", ""),
        help="TTF file to expose as /fonts/Roboto/Roboto-Regular.ttf on the generated disk",
    )
    run_p.add_argument("--limit", type=int, default=0, help="Limit rows after filtering; 0 means no limit")
    run_p.add_argument("--function", action="append", default=[], help="Only include this function; repeatable")
    run_p.add_argument("--supported-only", action="store_true", help="Drop unsupported findings from the run set")
    run_p.add_argument(
        "--llm-generate-unsupported",
        action="store_true",
        help="Ask the LLM to generate a bounded QEMU replay for unsupported findings",
    )
    run_p.add_argument(
        "--llm-limit",
        type=int,
        default=0,
        help="Maximum number of unsupported rows to send to the LLM; 0 means no extra cap",
    )
    run_p.add_argument(
        "--secret-env",
        default="/mnt/disk7/jw_bmc/secrets/openrouter.env",
        help="Optional env file to load OpenRouter key from without printing it",
    )
    run_p.add_argument(
        "--llm-model",
        default=os.environ.get("BMC_AGENT_LLM_MODEL", "anthropic/claude-sonnet-4.6"),
        help="Model for generated replay plans when env does not already override it",
    )
    run_p.add_argument("--keep-generated-worktree", action="store_true")

    args = parser.parse_args(argv)
    rows = collect_findings([Path(p) for p in args.findings])
    rows = filter_rows(rows, args)

    if args.cmd == "manifest":
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(output, rows)
        print(f"wrote {len(rows)} rows to {output}")
        return 0

    return run_rows(rows, args)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--findings",
        action="append",
        required=True,
        help="Directory containing BUG-*.md findings; repeatable",
    )


def collect_findings(dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for root in dirs:
        for path in sorted(root.glob("BUG-*.md")):
            row = parse_finding(path)
            if row:
                rows.append(row)
    return sorted(rows, key=lambda r: (not r["supported"], r["finding_set"], r["bug_id"], r["function"]))


def parse_finding(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    header = HEADER_RE.search(text)
    if not header:
        return None

    fields = {m.group("key").strip().lower(): clean_cell(m.group("value")) for m in FIELD_RE.finditer(text)}
    function = header.group("function")
    replay_case = CASE_BY_ENTRY.get(function)
    replay_rule = REPLAY_CATALOG.get(replay_case) if replay_case else None
    prop = PROPERTY_RE.search(text)

    return {
        "finding_set": path.parent.name,
        "finding_path": str(path),
        "bug_id": header.group("bug_id"),
        "function": function,
        "short_module": header.group("short_module"),
        "module": fields.get("module"),
        "confidence": fields.get("confidence"),
        "signal": fields.get("signal"),
        "realism": fields.get("realism"),
        "violated_property": prop.group("property") if prop else None,
        "supported": bool(replay_case),
        "replay_case": replay_case,
        "replay_category": replay_rule.category if replay_rule else None,
        "selection_rule": replay_rule.selection_rule if replay_rule else None,
        "expected_outcome": EXPECTED_OUTCOME_BY_CASE.get(replay_case or ""),
    }


def clean_cell(value: str) -> str:
    value = value.strip()
    value = re.sub(r"`([^`]*)`", r"\1", value)
    return value


def filter_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    funcs = set(getattr(args, "function", []) or [])
    if funcs:
        rows = [r for r in rows if r["function"] in funcs]
    if getattr(args, "supported_only", False):
        rows = [r for r in rows if r["supported"]]
    limit = getattr(args, "limit", 0) or 0
    if limit > 0:
        rows = rows[:limit]
    return rows


def run_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    qemu_root = output / "qemu"
    qemu_root.mkdir(exist_ok=True)
    write_jsonl(output / "manifest.jsonl", rows)

    results: list[dict[str, Any]] = []
    llm_generated = 0
    for index, row in enumerate(rows, 1):
        if not row["supported"]:
            llm_cap = getattr(args, "llm_limit", 0) or 0
            if getattr(args, "llm_generate_unsupported", False) and (llm_cap == 0 or llm_generated < llm_cap):
                llm_generated += 1
                result = run_llm_generated_row(row, index, output, qemu_root, args)
                results.append(result)
                append_jsonl(output / "results.jsonl", result)
                print(
                    f"[{index}/{len(rows)}] {row['bug_id']} {row['function']} -> "
                    f"{result['outcome']} llm_generated=true marker={result.get('marker')}",
                    flush=True,
                )
                continue
            result = {
                **row,
                "index": index,
                "outcome": "unsupported_by_current_replay_catalog",
                "marker": None,
                "target_event": None,
                "artifact_dir": None,
                "duration_sec": 0.0,
                "returncode": None,
                "llm_generated": False,
            }
            results.append(result)
            append_jsonl(output / "results.jsonl", result)
            continue

        result = run_supported_row(row, index, output, qemu_root, args)
        results.append(result)
        append_jsonl(output / "results.jsonl", result)
        print(
            f"[{index}/{len(rows)}] {row['bug_id']} {row['function']} -> "
            f"{result['outcome']} marker={result.get('marker')}",
            flush=True,
        )

    summary = summarize(results)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (output / "summary.md").write_text(render_summary_md(summary, results), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary["counts"].get("replay_error") else 1


def run_supported_row(
    row: dict[str, Any],
    index: int,
    output: Path,
    qemu_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    slug = safe_slug(f"{row['finding_set']}-{row['bug_id']}-{row['function']}")
    workdir = qemu_root / slug
    workdir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "entry_function": row["function"],
        "failing_property": row.get("violated_property"),
        "finding_path": row["finding_path"],
        "bug_id": row["bug_id"],
        "finding_set": row["finding_set"],
        "posthoc_output": str(output),
    }
    metadata_path = workdir / "posthoc_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

    env = os.environ.copy()
    env["BMC_AGENT_DYN_QEMU_METADATA"] = str(metadata_path)
    env["BMC_AGENT_DYN_QEMU_ENTRY"] = row["function"]
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "vibeos_qemu_dynamic_replay.py"),
        "--repo",
        str(Path(args.repo).resolve()),
        "--case",
        "auto",
        "--workdir",
        str(workdir),
        "--build-timeout",
        str(args.build_timeout),
        "--qemu-timeout",
        str(args.qemu_timeout),
    ]
    if getattr(args, "with_fat32_disk", False):
        cmd.append("--with-fat32-disk")
    if getattr(args, "font_file", ""):
        cmd.extend(["--font-file", str(args.font_file)])

    started = time.monotonic()
    timeout = args.build_timeout + args.qemu_timeout + 180
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = coerce_output(exc.stdout)
        stderr = coerce_output(exc.stderr) + f"\nposthoc wrapper timeout after {timeout}s\n"
        returncode = 124

    duration = time.monotonic() - started
    (workdir / "posthoc.stdout.log").write_text(stdout or "", encoding="utf-8", errors="replace")
    (workdir / "posthoc.stderr.log").write_text(stderr or "", encoding="utf-8", errors="replace")

    marker = read_marker(workdir, stdout)
    outcome = classify_marker(marker, returncode)
    return {
        **row,
        "index": index,
        "outcome": outcome,
        "marker": marker,
        "target_event": extract_target_event(marker),
        "artifact_dir": str(workdir),
        "duration_sec": round(duration, 3),
        "returncode": returncode,
        "llm_generated": False,
        "expected_matched": outcome == row.get("expected_outcome") if row.get("expected_outcome") else None,
    }


def run_llm_generated_row(
    row: dict[str, Any],
    index: int,
    output: Path,
    qemu_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    slug = safe_slug(f"{row['finding_set']}-{row['bug_id']}-{row['function']}-llm")
    workdir = qemu_root / slug
    workdir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "entry_function": row["function"],
        "failing_property": row.get("violated_property"),
        "finding_path": row["finding_path"],
        "bug_id": row["bug_id"],
        "finding_set": row["finding_set"],
        "posthoc_output": str(output),
        "llm_generated": True,
    }
    (workdir / "posthoc_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

    started = time.monotonic()
    plan_result = generate_llm_replay_plan(row, Path(args.repo), workdir, args)
    if not plan_result.get("accepted"):
        duration = time.monotonic() - started
        return {
            **row,
            "index": index,
            "outcome": plan_result.get("outcome", "llm_plan_rejected"),
            "marker": None,
            "target_event": None,
            "artifact_dir": str(workdir),
            "duration_sec": round(duration, 3),
            "returncode": None,
            "llm_generated": True,
            "llm_plan_error": plan_result.get("error"),
            "llm_plan": plan_result.get("plan"),
            "expected_matched": None,
        }

    plan = plan_result["plan"]
    result = run_generated_plan(row, index, Path(args.repo), output, workdir, plan, args)
    result["duration_sec"] = round(time.monotonic() - started, 3)
    return result


def generate_llm_replay_plan(
    row: dict[str, Any],
    vibeos_repo: Path,
    workdir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    prepare_llm_environment(args)
    prompt = build_llm_replay_prompt(row, vibeos_repo, args)
    (workdir / "llm_prompt.txt").write_text(prompt, encoding="utf-8", errors="replace")
    try:
        from bmc_agent.config import Config
        from bmc_agent.llm import LLMClient

        cfg = Config.from_env()
        llm = LLMClient(cfg)
        response = llm.complete(
            "You generate bounded C replay injections for VibeOS QEMU validation. Return only JSON.",
            prompt,
            max_tokens=2048,
            temperature=0.1,
            role="dynamic_repro",
        )
    except Exception as exc:
        return {"accepted": False, "outcome": "llm_error", "error": sanitize_error(str(exc))[:500]}

    (workdir / "llm_response.txt").write_text(response or "", encoding="utf-8", errors="replace")
    try:
        plan = parse_json_object(response)
    except Exception as exc:
        return {"accepted": False, "outcome": "llm_plan_rejected", "error": f"invalid JSON: {exc}", "plan": None}

    (workdir / "llm_plan.raw.json").write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    if plan.get("supported") is False:
        return {
            "accepted": False,
            "outcome": "llm_marked_unsupported",
            "error": str(plan.get("reason") or "LLM marked unsupported"),
            "plan": plan,
        }
    ok, error = validate_llm_plan(plan)
    if not ok:
        return {"accepted": False, "outcome": "llm_plan_rejected", "error": error, "plan": plan}
    (workdir / "llm_plan.accepted.json").write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    return {"accepted": True, "plan": plan}


def build_llm_replay_prompt(row: dict[str, Any], vibeos_repo: Path, args: argparse.Namespace | None = None) -> str:
    finding_text = Path(row["finding_path"]).read_text(encoding="utf-8", errors="replace")[:8000]
    source_context = (
        extract_source_context(vibeos_repo, row.get("module"), row["function"])
        + "\n\n"
        + extract_common_public_api_context(vibeos_repo)
    )[:14000]
    anchor_list = "\n".join(f"- {name}: insert immediately after `{anchor.strip()}`" for name, anchor in GENERATED_ANCHORS.items())
    disk_note = (
        "QEMU will attach a generated FAT32 virtio-blk disk containing "
        "`/fonts/Roboto/Roboto-Regular.ttf` and `/home/user`."
        if args is not None and getattr(args, "with_fat32_disk", False)
        else "QEMU will not attach an experiment-managed FAT32 disk unless the runner enables it."
    )
    return f"""Generate one VibeOS QEMU target replay plan for an existing BMC-Agent finding.

The tool will copy the VibeOS repo, patch only `kernel/kernel.c`, build with
`make TARGET=qemu PRINTF=uart`, boot with QEMU, and parse serial output markers.
{disk_note}

Plan policy:
- First decide whether this finding can be honestly exercised on TARGET=qemu
  using only code visible from `kernel/kernel.c` and its existing includes.
- If the target path depends on Pi-only hardware, QEMU HAL stubs, hidden static
  state, unavailable device queues, unavailable user programs, or an
  environment condition you cannot create from `kernel_main`, return
  `"supported": false`.
- If a function is static/internal but reachable through a public API after
  `vfs_init()` or `ttf_init()`, use the public API path instead of calling the
  static/internal function directly.
- Do not invent declarations with `extern` or modify private global state to
  force reachability. If a function or state is not visible through existing
  headers, the replay is unsupported.
- It is acceptable to generate a replay that ends in
  `VALIDATION:INCONCLUSIVE target_event=NO_TARGET_VERDICT ...` when the target
  returns a guard/error value or the unsafe state is absent on QEMU.

Hard safety rules:
- Return only JSON. Do not include markdown.
- You may only patch `kernel/kernel.c`.
- `anchor` must be one of the allowed anchor labels below.
- `c_injection` must be C statements inserted inside `kernel_main`, not a full file.
- Do not use shell, filesystem host paths, `#include`, infinite loops, or build commands.
- Prefer public APIs and headers already included by `kernel/kernel.c`.
- Only call functions, types, and macros that are declared in the provided
  source/header context or in headers already included by `kernel/kernel.c`;
  do not guess helper API names. If the setup API you need is not shown, return
  `"supported": false`.
- Do not call `stbtt_*` functions directly from `kernel_main`; they are
  internal to `kernel/ttf.c`. Use `ttf_init`, `ttf_get_glyph`,
  `ttf_get_metrics`, `ttf_get_advance`, or `ttf_get_kerning`.
- Do not call private `kapi_*` helper functions directly. Use public kernel
  APIs such as `vfs_create`, `vfs_lookup`, `vfs_read`, `vfs_write`, and
  `vfs_append`, or use the `kapi.<field>` table after `kapi_init`.
- If the function is static/internal or cannot be honestly exercised on TARGET=qemu, set `"supported": false`.
- Print `[BMC-DYN] ...` diagnostics for setup, safe guards, and ordinary return values.
- If the replay reaches a concrete unsafe operation but VibeOS will not fault, print:
  `DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=<EVENT> <short detail>`
- If the replay can detect a semantic mismatch directly, print:
  `DYNAMIC:CONFIRMED target_event=<EVENT> <short detail>`
- Do not print `DYNAMIC:CONFIRMED` for a safe control path or for a call that
  merely returns without fault. Use `[BMC-DYN] ...` diagnostics for those paths.
- `DYNAMIC:CONFIRMED` is only for a target fault, a directly observed semantic
  mismatch, or a directly observed unsafe acceptance of invalid input.
- If the target returns an error/guard value such as `-1`, or the condition is
  absent on QEMU, print `VALIDATION:INCONCLUSIVE target_event=NO_TARGET_VERDICT ...`.
- Emit at most one verdict marker (`DYNAMIC:` or `VALIDATION:`) on any executed path.
- Verdict markers must be emitted by `printf("DYNAMIC:...\\n")` or
  `printf("VALIDATION:...\\n")`; never write raw `DYNAMIC:` or `VALIDATION:`
  tokens as C statements.
- If the target is expected to panic/data-abort before your marker, print a `[BMC-DYN] case ... start` line first; the tool will classify target panic.

Allowed anchors:
{anchor_list}

JSON schema:
{{
  "supported": true,
  "reason": "short reason",
  "patch_file": "kernel/kernel.c",
  "anchor": "after_kapi_init_log",
  "target_event": "UPPER_SNAKE_CASE_EVENT",
  "expected_outcome": "qemu_confirmed or observed_safety_concern or inconclusive",
  "c_injection": "C statements to insert"
}}

If unsupported:
{{
  "supported": false,
  "reason": "why this cannot be replayed honestly on TARGET=qemu"
}}

Finding metadata:
- set: {row.get('finding_set')}
- bug_id: {row.get('bug_id')}
- function: {row.get('function')}
- module: {row.get('module')}
- violated_property: {row.get('violated_property')}
- original confidence: {row.get('confidence')}
- original signal: {row.get('signal')}

Finding report:
```markdown
{finding_text}
```

Relevant source context:
```c
{source_context}
```
"""


def extract_source_context(vibeos_repo: Path, module: str | None, function: str) -> str:
    if not module:
        return ""
    path = vibeos_repo / module
    if not path.exists() or not path.is_file():
        return ""
    header_text = ""
    header = path.with_suffix(".h")
    if header.exists() and header.is_file():
        header_text = header.read_text(encoding="utf-8", errors="replace")[:5000]
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    candidates = [i for i, line in enumerate(lines) if function in line]
    if not candidates:
        source_text = text[:9000]
        if header_text:
            return f"/* Header: {header.relative_to(vibeos_repo)} */\n{header_text}\n\n/* Source: {module} */\n{source_text}"
        return source_text
    # Prefer a likely definition over a prototype or call site.
    idx = candidates[0]
    for i in candidates:
        window = "\n".join(lines[i:min(len(lines), i + 4)])
        if "{" in window and not lines[i].lstrip().startswith("//"):
            idx = i
            break
    start = max(0, idx - 80)
    end = min(len(lines), idx + 140)
    numbered = [f"{n + 1}: {lines[n]}" for n in range(start, end)]
    source_text = "\n".join(numbered)
    if header_text:
        return f"/* Header: {header.relative_to(vibeos_repo)} */\n{header_text}\n\n/* Source excerpt: {module} */\n{source_text}"
    return source_text


def extract_common_public_api_context(vibeos_repo: Path) -> str:
    chunks = []
    for rel in ("kernel/vfs.h", "kernel/ttf.h", "kernel/kapi.h"):
        path = vibeos_repo / rel
        if path.exists() and path.is_file():
            chunks.append(f"/* Public header: {rel} */\n{path.read_text(encoding='utf-8', errors='replace')[:5000]}")
    return "\n\n".join(chunks)


def parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("top-level JSON is not an object")
    return value


def validate_llm_plan(plan: dict[str, Any]) -> tuple[bool, str | None]:
    if plan.get("supported") is not True:
        return False, str(plan.get("reason") or "LLM marked unsupported")
    if plan.get("patch_file") != "kernel/kernel.c":
        return False, "patch_file must be kernel/kernel.c"
    anchor = plan.get("anchor")
    if anchor not in GENERATED_ANCHORS:
        return False, f"unsupported anchor: {anchor}"
    injection = plan.get("c_injection")
    if not isinstance(injection, str) or not injection.strip():
        return False, "missing c_injection"
    if len(injection) > 6000:
        return False, "c_injection too large"
    lowered = injection.lower()
    for banned in BANNED_INJECTION_PATTERNS:
        if banned.lower() in lowered:
            return False, f"banned token in c_injection: {banned}"
    if "[BMC-DYN]" not in injection and "DYNAMIC:" not in injection:
        return False, "c_injection must emit a BMC-DYN or DYNAMIC marker"
    unquoted_marker = _invalid_unquoted_marker_reason(injection)
    if unquoted_marker:
        return False, unquoted_marker
    invalid = _invalid_confirmed_marker_reason(injection)
    if invalid:
        return False, invalid
    invalid_call = _invalid_generated_call_reason(injection)
    if invalid_call:
        return False, invalid_call
    return True, None


def run_generated_plan(
    row: dict[str, Any],
    index: int,
    vibeos_repo: Path,
    output: Path,
    workdir: Path,
    plan: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    replay_root = workdir / "vibeos_replay"
    if replay_root.exists():
        shutil.rmtree(replay_root)
    _ensure_tlse_submodule(vibeos_repo)
    _copy_vibeos_repo(vibeos_repo, replay_root)

    patch_result = apply_generated_kernel_patch(replay_root, plan)
    (workdir / "generated_replay.diff").write_text(patch_result["diff"], encoding="utf-8", errors="replace")
    if not patch_result["ok"]:
        return generated_result(row, index, workdir, "llm_plan_rejected", None, None, None, "patch failed")

    disk_img = _prepare_qemu_disk_image(replay_root, workdir, args)

    build = _run(_vibeos_build_command(enable_replay=True), cwd=replay_root, timeout=args.build_timeout)
    (workdir / "vibeos_build.stdout.log").write_text(build.stdout or "", encoding="utf-8", errors="replace")
    (workdir / "vibeos_build.stderr.log").write_text(build.stderr or "", encoding="utf-8", errors="replace")
    if build.returncode != 0:
        cleanup_generated_worktree(replay_root, args)
        reason = summarize_build_failure(build.stderr or "", build.stdout or "")
        return generated_result(row, index, workdir, "replay_error", None, None, build.returncode, reason)

    qemu_bin = shutil.which(os.environ.get("BMC_AGENT_VIBEOS_QEMU_BIN", "qemu-system-aarch64"))
    if not qemu_bin:
        cleanup_generated_worktree(replay_root, args)
        return generated_result(row, index, workdir, "replay_error", None, None, None, "qemu binary not found")

    kernel_bin = replay_root / "build" / "vibeos.bin"
    qemu = _run(_qemu_command(qemu_bin, kernel_bin, disk_img), cwd=replay_root, timeout=args.qemu_timeout)
    combined = (qemu.stdout or "") + "\n" + (qemu.stderr or "")
    (workdir / "vibeos_qemu.stdout.log").write_text(qemu.stdout or "", encoding="utf-8", errors="replace")
    (workdir / "vibeos_qemu.stderr.log").write_text(qemu.stderr or "", encoding="utf-8", errors="replace")
    (workdir / "vibeos_qemu.tail.log").write_text(_tail(combined, limit=12000), encoding="utf-8", errors="replace")

    marker = _first_marker_line(combined)
    if not marker and _has_target_fault(combined):
        marker = "DYNAMIC:CONFIRMED target_event=TARGET_PANIC"
    if not marker and "[BMC-DYN]" in combined:
        marker = "VALIDATION:INCONCLUSIVE target_event=NO_TARGET_VERDICT generated replay emitted diagnostics but no verdict marker"
    if marker:
        (workdir / "validation_marker.txt").write_text(marker + "\n", encoding="utf-8")
    outcome = classify_marker(marker, qemu.returncode)
    cleanup_generated_worktree(replay_root, args)
    return generated_result(row, index, workdir, outcome, marker, extract_target_event(marker), qemu.returncode, None)


def apply_generated_kernel_patch(replay_root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    kernel_c = replay_root / "kernel" / "kernel.c"
    before = kernel_c.read_text(encoding="utf-8")
    anchor = GENERATED_ANCHORS[plan["anchor"]]
    if anchor not in before:
        return {"ok": False, "diff": ""}
    injection = _guard_replay_injection(plan["c_injection"])
    after = before.replace(anchor, anchor + injection, 1)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="kernel/kernel.c.before",
            tofile="kernel/kernel.c.after",
        )
    )
    kernel_c.write_text(after, encoding="utf-8")
    return {"ok": True, "diff": diff}


def generated_result(
    row: dict[str, Any],
    index: int,
    workdir: Path,
    outcome: str,
    marker: str | None,
    target_event: str | None,
    returncode: int | None,
    failure_mode: str | None,
) -> dict[str, Any]:
    return {
        **row,
        "index": index,
        "outcome": outcome,
        "marker": marker,
        "target_event": target_event,
        "artifact_dir": str(workdir),
        "returncode": returncode,
        "llm_generated": True,
        "replay_case": "llm_generated",
        "replay_category": "llm_generated",
        "selection_rule": "LLM-generated bounded target replay",
        "failure_mode": failure_mode,
        "expected_matched": None,
    }


def cleanup_generated_worktree(replay_root: Path, args: argparse.Namespace) -> None:
    if getattr(args, "keep_generated_worktree", False):
        return
    shutil.rmtree(replay_root, ignore_errors=True)


def prepare_llm_environment(args: argparse.Namespace) -> None:
    secret_env = Path(getattr(args, "secret_env", "") or "")
    secret_values: dict[str, str] = {}
    if secret_env.exists():
        secret_values = read_env_file(secret_env)
        for key, value in secret_values.items():
            os.environ[key] = value
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key and not os.environ.get("BMC_AGENT_LLM_API_KEY"):
        os.environ["BMC_AGENT_LLM_API_KEY"] = openrouter_key
    if secret_values.get("OPENROUTER_API_KEY"):
        os.environ["BMC_AGENT_LLM_API_KEY"] = secret_values["OPENROUTER_API_KEY"]
    os.environ.setdefault("BMC_AGENT_LLM_PROVIDER", "openai")
    os.environ.setdefault("BMC_AGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    os.environ.setdefault("BMC_AGENT_LLM_MODEL", getattr(args, "llm_model", "") or "anthropic/claude-sonnet-4.6")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def sanitize_error(text: str) -> str:
    text = re.sub(r"https://openrouter\.ai/workspaces/[^\"'\s]+/keys/[A-Za-z0-9_-]+", "https://openrouter.ai/workspaces/<redacted>/keys/<redacted>", text)
    text = re.sub(r"\bsk-or-v1-[A-Za-z0-9_-]+\b", "sk-or-v1-<redacted>", text)
    text = re.sub(r"\bsk-ant-[A-Za-z0-9_-]+\b", "sk-ant-<redacted>", text)
    return text


def read_marker(workdir: Path, stdout: str) -> str | None:
    marker_path = workdir / "validation_marker.txt"
    if marker_path.exists():
        return marker_path.read_text(encoding="utf-8", errors="replace").strip() or None
    marker = None
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("DYNAMIC:") or stripped.startswith("VALIDATION:"):
            marker = stripped
    return marker


def classify_marker(marker: str | None, returncode: int) -> str:
    if marker:
        if _invalid_confirmed_marker_reason(marker):
            return "inconclusive"
        if marker.startswith("DYNAMIC:CONFIRMED"):
            return "qemu_confirmed"
        if marker.startswith("DYNAMIC:OBSERVED_SAFETY_CONCERN"):
            return "observed_safety_concern"
        if marker.startswith("VALIDATION:PASS"):
            return "validation_pass"
        if marker.startswith("VALIDATION:INCONCLUSIVE"):
            return "inconclusive"
    if returncode == 0:
        return "no_marker"
    return "replay_error"


def extract_target_event(marker: str | None) -> str | None:
    if not marker:
        return None
    match = TARGET_EVENT_RE.search(marker)
    return match.group("event") if match else None


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(r["outcome"] for r in results)
    expected = [r for r in results if r.get("expected_outcome")]
    catalog_supported = sum(1 for r in results if r.get("supported"))
    llm_generated = sum(1 for r in results if r.get("llm_generated"))
    unsupported = sum(
        1
        for r in results
        if not r.get("supported")
        and not r.get("llm_generated")
        and r.get("outcome") == "unsupported_by_current_replay_catalog"
    )
    return {
        "total_rows": len(results),
        "catalog_supported_rows": catalog_supported,
        "llm_generated_rows": llm_generated,
        "validation_attempted_rows": catalog_supported + llm_generated,
        "unsupported_rows": unsupported,
        "counts": dict(sorted(counts.items())),
        "expected_checked": len(expected),
        "expected_matched": sum(1 for r in expected if r.get("expected_matched") is True),
    }


def render_summary_md(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [
        "# VibeOS post-hoc QEMU validation",
        "",
        "This run validates existing findings against the current target replay catalog. "
        "Unsupported rows are not negative evidence.",
        "",
        "## Summary",
        "",
        f"- Total rows: {summary['total_rows']}",
        f"- Catalog-supported rows: {summary['catalog_supported_rows']}",
        f"- LLM-generated rows: {summary['llm_generated_rows']}",
        f"- Validation-attempted rows: {summary['validation_attempted_rows']}",
        f"- Unsupported rows: {summary['unsupported_rows']}",
        f"- Expected matched: {summary['expected_matched']}/{summary['expected_checked']}",
        "",
        "## Outcome Counts",
        "",
    ]
    for key, value in summary["counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(
        [
            "",
            "## Attempted Results",
            "",
            "| Finding | Function | Source | Outcome | Target event | Expected | Artifact |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for r in results:
        if not r.get("supported") and not r.get("llm_generated"):
            continue
        artifact = r.get("artifact_dir") or ""
        source = "llm" if r.get("llm_generated") else "catalog"
        lines.append(
            f"| `{r['finding_set']}/{r['bug_id']}` | `{r['function']}` | `{source}` | "
            f"`{r['outcome']}` | `{r.get('target_event') or ''}` | "
            f"`{r.get('expected_outcome') or ''}` | `{artifact}` |"
        )
    positives = [r for r in results if r.get("outcome") in {"qemu_confirmed", "observed_safety_concern"}]
    if positives:
        lines.extend(
            [
                "",
                "## Positive Evidence",
                "",
                "| Finding | Function | Outcome | Evidence marker |",
                "|---|---|---|---|",
            ]
        )
        for r in positives:
            lines.append(
                f"| `{r['finding_set']}/{r['bug_id']}` | `{r['function']}` | "
                f"`{r['outcome']}` | {md_cell(r.get('marker') or '')} |"
            )
    detail_rows = [
        r
        for r in results
        if r.get("outcome")
        in {
            "llm_marked_unsupported",
            "llm_plan_rejected",
            "inconclusive",
            "no_marker",
            "replay_error",
            "validation_pass",
            "unsupported_by_current_replay_catalog",
        }
    ]
    if detail_rows:
        lines.extend(
            [
                "",
                "## Unsupported And Inconclusive Details",
                "",
                "| Finding | Function | Outcome | Concrete reason | Artifact |",
                "|---|---|---|---|---|",
            ]
        )
        for r in detail_rows:
            reason = concrete_reason(r)
            artifact = r.get("artifact_dir") or ""
            lines.append(
                f"| `{r['finding_set']}/{r['bug_id']}` | `{r['function']}` | "
                f"`{r['outcome']}` | {md_cell(reason)} | `{artifact}` |"
            )
    lines.append("")
    return "\n".join(lines)


def concrete_reason(row: dict[str, Any]) -> str:
    for key in ("llm_plan_error", "failure_mode", "marker"):
        value = row.get(key)
        if value:
            return str(value)
    if row.get("outcome") == "unsupported_by_current_replay_catalog":
        return "No catalog replay rule and LLM generation was not enabled for this row."
    if row.get("outcome") == "replay_error":
        rc = row.get("returncode")
        if rc is not None:
            return f"Replay command exited with return code {rc}; inspect build/QEMU logs."
        return "Replay failed before producing a verdict marker; inspect artifact logs."
    return ""


def summarize_build_failure(stderr: str, stdout: str) -> str:
    combined = "\n".join(part for part in (stderr, stdout) if part)
    interesting: list[str] = []
    for line in combined.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("undefined reference", "implicit declaration", "error:", "make: ***")):
            interesting.append(line.strip())
        if len(interesting) >= 4:
            break
    if interesting:
        return "generated build failed: " + " ; ".join(interesting)
    return "generated build failed; inspect vibeos_build.stderr.log and vibeos_build.stdout.log"


def md_cell(value: str, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    text = text.replace("|", "\\|")
    return text


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def coerce_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
