#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
BASE = REPO / "artifacts/autospec_251"
AUTOSPEC_BENCH = Path(os.environ.get("AUTOSPEC_BENCHMARK_ROOT", "benchmark"))

ROOTS = {
    "frama-c-problems": AUTOSPEC_BENCH / "frama-c-problems",
    "fib_46": AUTOSPEC_BENCH / "fib_46_benchmark",
    "svcomp": AUTOSPEC_BENCH / "SVCOMP",
    "code2inv_133": AUTOSPEC_BENCH / "code2inv_133_benchmark",
}

MAX_WORKERS = int(os.environ.get("AUTOSPEC251_JOBS", "12"))
MAX_ATTEMPTS = int(os.environ.get("AUTOSPEC251_MAX_ATTEMPTS", "5"))
CASE_TIMEOUT = int(os.environ.get("AUTOSPEC251_CASE_TIMEOUT", "900"))
POSTCHECK_TIMEOUT = int(os.environ.get("AUTOSPEC251_POSTCHECK_TIMEOUT", "180"))
LIMIT = int(os.environ.get("AUTOSPEC251_LIMIT", "0"))
MODEL = os.environ.get("BMC_AGENT_LLM_DEFAULT_MODEL", "anthropic/claude-sonnet-4.5")

SECRET_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]
WP_RE = re.compile(r"Frama-C/WP: (?:CONFIRMED — )?proved\s+(\d+)/(\d+)\s+goals")
PROVED_RE = re.compile(r"\[wp\]\s+Proved goals:\s+(\d+)\s*/\s*(\d+)")
UNPROVED_RE = re.compile(r"\[wp\]\s+\[(?:Failed|Timeout|Unknown|Qed|Alt-Ergo|Z3)[^\]]*\]\s+([A-Za-z0-9_@.$:-]+)")
FUNC_DEF_RE = re.compile(r"(?:^|[;}\s])([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{", re.M)
RESULT_RE = re.compile(r"^RESULT:\s+(.+)$", re.MULTILINE)
POSTCHECK_BUILDER = None
POSTCHECK_BUILDER_LOCK = threading.Lock()

EXCLUDE_NAME_PARTS = (
    "_marked",
    "_infilled",
    "_verified",
    "_final_simplified",
    "_gen_",
    "_merged",
)


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text


def matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    quote = ""
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def infer_entry(source: str) -> str:
    best = ""
    for m in FUNC_DEF_RE.finditer(source):
        name = m.group(1)
        if name in {"if", "while", "for", "switch", "do", "else"}:
            continue
        open_brace = source.find("{", m.end() - 1)
        close = matching_brace(source, open_brace)
        if close < 0:
            continue
        body = source[open_brace + 1 : close]
        if (
            re.search(r"\b(?:static_assert|_Static_assert|assert|__VERIFIER_assert)\s*\(", body)
            or "//@ assert" in body
            or "reach_error(" in body
        ):
            return name
        if name == "main":
            best = name
    return best or "main"


def discover_cases() -> list[dict]:
    rows: list[dict] = []
    for benchmark, root in ROOTS.items():
        for src in sorted(root.rglob("*.c")):
            if any(part in src.name for part in EXCLUDE_NAME_PARTS):
                continue
            rel = src.relative_to(root)
            if benchmark == "frama-c-problems":
                family = rel.parts[0] if rel.parts else ""
            else:
                family = benchmark
            source = src.read_text(errors="ignore")
            rows.append(
                {
                    "benchmark": benchmark,
                    "family": family,
                    "relative_path": str(rel),
                    "case_id": f"{benchmark}__{str(rel.with_suffix('')).replace('/', '__')}",
                    "source": str(src),
                    "entry": infer_entry(source),
                }
            )
    if LIMIT:
        rows = rows[:LIMIT]
    return rows


def parse_agent_log(path: Path) -> dict:
    raw = path.read_text(errors="replace") if path.exists() else ""
    contains_secret = any(pattern.search(raw) for pattern in SECRET_PATTERNS)
    clean = redact(raw)
    if clean != raw:
        path.write_text(clean)
    proved = total = None
    wp_matches = WP_RE.findall(clean)
    if wp_matches:
        proved, total = map(int, wp_matches[-1])
    result = ""
    m = RESULT_RE.search(clean)
    if m:
        result = m.group(1).strip()
    failure_mode = ""
    if "LLMError" in clean or "LLM request failed" in clean:
        failure_mode = "llm_error"
    elif "Traceback" in clean:
        failure_mode = "python_exception"
    elif "frama-c" in clean and "not on PATH" in clean:
        failure_mode = "frama_c_unavailable"
    elif "TIMEOUT after" in clean:
        failure_mode = "timeout"
    elif wp_matches and total == 0:
        failure_mode = "wp_zero_goals"
    elif wp_matches and proved != total:
        failure_mode = "wp_unproved_goals"
    elif not wp_matches and "RESULT: SATISFIED" not in clean:
        failure_mode = "not_satisfied"
    return {
        "agent_result": result,
        "agent_wp_proved": proved,
        "agent_wp_total": total,
        "contains_secret_like_value": contains_secret,
        "failure_mode": failure_mode,
    }


def artifact_files(case_dir: Path) -> dict:
    jsons = sorted(case_dir.glob("synthesized_specs_*.json")) + sorted(
        case_dir.glob("synthesized_loop_invariants_*.json")
    )
    cands = []
    for name in ("postcheck_annotated.c", "source_instrumented.c"):
        p = case_dir / name
        if p.exists():
            cands.append(p)
    cands += sorted(case_dir.glob("*_instrumented.c"))
    return {
        "json": str(jsons[0]) if jsons else "",
        "annotated_or_instrumented": str(cands[0]) if cands else "",
    }


def build_postcheck_target(case_dir: Path) -> Path | None:
    """Materialize an annotated C file from BMC-Agent JSON artifacts when needed."""
    global POSTCHECK_BUILDER
    if POSTCHECK_BUILDER is None:
        with POSTCHECK_BUILDER_LOCK:
            if POSTCHECK_BUILDER is None:
                module_path = Path(__file__).with_name("_autospec_postcheck.py")
                spec = importlib.util.spec_from_file_location("autospec251_postcheck_builder", module_path)
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"failed to load {module_path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                POSTCHECK_BUILDER = module
    return POSTCHECK_BUILDER.build_annotated_source(case_dir)


def run_postcheck(case_dir: Path) -> dict:
    try:
        target_path = build_postcheck_target(case_dir)
    except Exception as exc:
        return {
            "status": "error",
            "n_proved": None,
            "n_total": None,
            "unproved": [],
            "log": "",
            "failure_mode": f"build_postcheck_target:{type(exc).__name__}",
        }
    if target_path is None:
        files = artifact_files(case_dir)
        target = files["annotated_or_instrumented"]
        target_path = Path(target) if target else None
    if target_path is None:
        return {"status": "missing_artifact", "n_proved": None, "n_total": None, "unproved": [], "log": ""}
    target_path = Path(target_path)
    if not target_path.is_absolute():
        target_path = (REPO / target_path).resolve()
    else:
        target_path = target_path.resolve()
    try:
        target_path.chmod(0o644)
        for parent in (BASE, case_dir, target_path.parent):
            parent.chmod(0o755)
        for parent in target_path.parents:
            try:
                if parent == REPO.resolve().parent:
                    break
                if parent.is_relative_to(REPO.resolve()):
                    parent.chmod(0o755)
            except AttributeError:
                try:
                    parent.relative_to(REPO.resolve())
                except ValueError:
                    continue
                parent.chmod(0o755)
    except OSError:
        pass
    try:
        frama_target = target_path.relative_to(REPO.resolve())
    except ValueError:
        frama_target = target_path
    log_path = case_dir / "postcheck_framac.log"
    cmd = [
        "frama-c",
        "-wp",
        "-wp-precond-weakening",
        "-wp-no-callee-precond",
        "-wp-prover",
        "Alt-Ergo,Z3",
        "-wp-print",
        "-wp-timeout",
        "8",
        str(frama_target),
    ]
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=POSTCHECK_TIMEOUT,
            env=os.environ.copy(),
        )
        raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
        exit_code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or "") + "\n" + (exc.stderr or "") + f"\nTIMEOUT after {POSTCHECK_TIMEOUT}s\n"
        exit_code = 124
        timed_out = True
    raw = redact(raw)
    log_path.write_text(raw)
    matches = PROVED_RE.findall(raw)
    proved = total = None
    if matches:
        proved, total = map(int, matches[-1])
    unproved = UNPROVED_RE.findall(raw)
    if timed_out:
        status = "timeout"
    elif "annot-error" in raw or "User Error" in raw or "syntax error" in raw.lower():
        status = "invalid"
    elif total and total > 0 and proved == total:
        status = "pass"
    elif total and total > 0:
        status = "fail"
    else:
        status = "zero_goals" if exit_code == 0 else "error"
    return {
        "status": status,
        "n_proved": proved,
        "n_total": total,
        "unproved": unproved,
        "exit_code": exit_code,
        "runtime_sec": round(time.time() - start, 2),
        "log": str(log_path),
    }


def attempt_score(attempt: dict) -> tuple:
    pc = attempt.get("postcheck") or {}
    if pc.get("status") == "pass":
        return (5, 1.0, pc.get("n_total") or 0, -attempt.get("attempt", 0))
    total = pc.get("n_total") or 0
    proved = pc.get("n_proved") or 0
    ratio = proved / total if total else 0.0
    rank = {"fail": 4, "invalid": 3, "timeout": 2, "zero_goals": 1, "missing_artifact": 0, "error": 0}
    return (rank.get(pc.get("status"), 0), ratio, total, -attempt.get("attempt", 0))


def run_attempt(case: dict, case_dir: Path, attempt_no: int) -> dict:
    attempt_dir = case_dir / f"attempt_{attempt_no}"
    if attempt_dir.exists():
        shutil.rmtree(attempt_dir)
    attempt_dir.mkdir(parents=True)
    attempt_dir.chmod(0o755)
    copied_source = attempt_dir / "source.c"
    shutil.copy2(case["source"], copied_source)
    copied_source.chmod(0o644)
    log_path = attempt_dir / "run.log"
    cmd = [
        sys.executable,
        "-m",
        "bmc_agent.cli",
        "verify",
        "--source",
        str(copied_source),
        "--driver",
        "specsbench",
        "--entry",
        case["entry"],
        "--agentic",
        "--specs-bench",
        "--oracle",
        "frama-c",
        "--provider",
        "openai",
        "--model",
        MODEL,
        "--output",
        str(attempt_dir),
    ]
    start = time.time()
    try:
        with log_path.open("w") as log:
            proc = subprocess.run(
                cmd,
                cwd=REPO,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=CASE_TIMEOUT,
                env=os.environ.copy(),
            )
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        exit_code = 124
        with log_path.open("a") as log:
            log.write(f"\nTIMEOUT after {CASE_TIMEOUT}s\n")
    runtime = round(time.time() - start, 2)
    parsed = parse_agent_log(log_path)
    postcheck = run_postcheck(attempt_dir)
    status = "pass" if postcheck.get("status") == "pass" else "fail"
    if exit_code == 124:
        status = "timeout"
    elif parsed["failure_mode"] in {"llm_error", "python_exception", "frama_c_unavailable"}:
        status = "error"
    return {
        "attempt": attempt_no,
        "status": status,
        "exit_code": exit_code,
        "runtime_sec": runtime,
        "log": str(log_path),
        "artifact_dir": str(attempt_dir),
        "postcheck": postcheck,
        **parsed,
        **artifact_files(attempt_dir),
    }


def promote_attempt(attempt: dict, case_dir: Path) -> None:
    src_dir = Path(attempt["artifact_dir"])
    for p in src_dir.iterdir():
        if p.is_file() and (
            p.name in {"run.log", "source.c", "postcheck_annotated.c", "source_instrumented.c", "postcheck_framac.log"}
            or p.name.startswith("synthesized_")
            or p.name.endswith("_instrumented.c")
        ):
            shutil.copy2(p, case_dir / p.name)


def repeated_nonpass(attempts: list[dict], n: int = 3) -> bool:
    if len(attempts) < n:
        return False
    recent = attempts[-n:]
    sigs = [
        (
            a["postcheck"].get("status"),
            a["postcheck"].get("n_proved"),
            a["postcheck"].get("n_total"),
            a.get("failure_mode"),
        )
        for a in recent
    ]
    return len(set(sigs)) == 1 and sigs[-1][0] != "pass"


def run_case(case: dict) -> dict:
    case_dir = BASE / "cases" / case["case_id"]
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    case_dir.chmod(0o755)
    attempts = []
    for attempt_no in range(1, MAX_ATTEMPTS + 1):
        attempt = run_attempt(case, case_dir, attempt_no)
        attempts.append(attempt)
        pc = attempt["postcheck"]
        print(
            f"attempt {attempt_no}/{MAX_ATTEMPTS} {case['benchmark']}/{case['relative_path']} "
            f"agent={attempt['status']} pc={pc.get('status')} wp={pc.get('n_proved')}/{pc.get('n_total')}",
            flush=True,
        )
        if pc.get("status") == "pass":
            break
        if repeated_nonpass(attempts):
            print(
                f"early-stop {case['benchmark']}/{case['relative_path']} after {len(attempts)} repeated",
                flush=True,
            )
            break
    selected = max(attempts, key=attempt_score)
    promote_attempt(selected, case_dir)
    files = artifact_files(case_dir)
    pc = selected["postcheck"]
    status = "pass" if pc.get("status") == "pass" else selected["status"]
    failure_mode = selected["failure_mode"]
    if status != "pass":
        failure_mode = pc.get("status") or failure_mode
    return {
        **case,
        "status": status,
        "attempts_used": len(attempts),
        "selected_attempt": selected["attempt"],
        "runtime_sec": round(sum(a["runtime_sec"] for a in attempts), 2),
        "agent_result": selected["agent_result"],
        "agent_wp_proved": selected["agent_wp_proved"],
        "agent_wp_total": selected["agent_wp_total"],
        "postcheck_status": pc.get("status"),
        "postcheck_proved": pc.get("n_proved"),
        "postcheck_total": pc.get("n_total"),
        "postcheck_unproved": pc.get("unproved", []),
        "failure_mode": failure_mode,
        "attempts": attempts,
        "contains_secret_like_value": any(a["contains_secret_like_value"] for a in attempts),
        "artifact_dir": str(case_dir),
        "log": str(case_dir / "run.log"),
        "postcheck_log": str(case_dir / "postcheck_framac.log"),
        **files,
    }


def write_reports(rows: list[dict], cases: list[dict], partial: bool) -> None:
    rows = sorted(rows, key=lambda r: (r["benchmark"], r["relative_path"]))
    counts: dict[str, int] = {}
    by_benchmark: dict[str, dict[str, int]] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        b = by_benchmark.setdefault(r["benchmark"], {"total": 0})
        b["total"] += 1
        b[r["status"]] = b.get(r["status"], 0) + 1
    report = {
        "label": "AutoSpec 251 rerun after BMC-Agent ACSL/WP fixes",
        "model": MODEL,
        "workers": MAX_WORKERS,
        "case_timeout_sec": CASE_TIMEOUT,
        "postcheck_timeout_sec": POSTCHECK_TIMEOUT,
        "max_attempts": MAX_ATTEMPTS,
        "partial": partial,
        "progress": f"{len(rows)}/{len(cases)}",
        "counts": counts,
        "by_benchmark": by_benchmark,
        "rows": rows,
    }
    (BASE / "report.json").write_text(json.dumps(report, indent=2))
    lines = [
        "# AutoSpec 251 Rerun After BMC-Agent ACSL/WP Fixes",
        "",
        f"- Progress: `{len(rows)}/{len(cases)}`",
        f"- Model: `{MODEL}`",
        f"- Workers: `{MAX_WORKERS}`",
        f"- Max attempts: `{MAX_ATTEMPTS}`",
        f"- Counts: `{counts}`",
        "",
        "## By Benchmark",
        "",
        "| Benchmark | Total | Pass | Fail | Timeout | Error |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for bench, c in sorted(by_benchmark.items()):
        lines.append(
            f"| `{bench}` | {c.get('total', 0)} | {c.get('pass', 0)} | {c.get('fail', 0)} | "
            f"{c.get('timeout', 0)} | {c.get('error', 0)} |"
        )
    lines += [
        "",
        "## Cases",
        "",
        "| Benchmark | Case | Status | Postcheck WP | Attempts | Runtime | Failure | Artifacts |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for r in rows:
        pc_wp = "" if r.get("postcheck_total") is None else f"{r.get('postcheck_proved')}/{r.get('postcheck_total')}"
        artifacts = f"[log]({r['log']}) [postcheck]({r['postcheck_log']})"
        if r.get("json"):
            artifacts += f" [json]({r['json']})"
        if r.get("annotated_or_instrumented"):
            artifacts += f" [c]({r['annotated_or_instrumented']})"
        lines.append(
            f"| `{r['benchmark']}` | `{r['relative_path']}` | {r['status']} | {pc_wp} | "
            f"{r['attempts_used']} | {r['runtime_sec']}s | {r.get('failure_mode','')} | {artifacts} |"
        )
    (BASE / "summary.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    BASE.chmod(0o755)
    (BASE / "cases").mkdir(exist_ok=True)
    (BASE / "cases").chmod(0o755)
    build_postcheck_target(BASE / "__preload__")
    cases = discover_cases()
    (BASE / "manifest.json").write_text(json.dumps(cases, indent=2))
    print(f"Discovered {len(cases)} cases; workers={MAX_WORKERS}; attempts={MAX_ATTEMPTS}", flush=True)
    rows: list[dict] = []
    write_reports(rows, cases, partial=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(run_case, case): case for case in cases}
        for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
            case = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    **case,
                    "status": "error",
                    "attempts_used": 0,
                    "selected_attempt": 0,
                    "runtime_sec": 0,
                    "agent_result": "",
                    "agent_wp_proved": None,
                    "agent_wp_total": None,
                    "postcheck_status": "error",
                    "postcheck_proved": None,
                    "postcheck_total": None,
                    "postcheck_unproved": [],
                    "failure_mode": f"runner_exception:{type(exc).__name__}",
                    "attempts": [],
                    "contains_secret_like_value": False,
                    "artifact_dir": str(BASE / "cases" / case["case_id"]),
                    "log": "",
                    "postcheck_log": "",
                    "json": "",
                    "annotated_or_instrumented": "",
                }
            rows.append(row)
            print(
                f"[{idx:03d}/{len(cases)}] {row['benchmark']}/{row['relative_path']} -> "
                f"{row['status']} {row.get('postcheck_proved')}/{row.get('postcheck_total')}",
                flush=True,
            )
            write_reports(rows, cases, partial=idx < len(cases))
    write_reports(rows, cases, partial=False)
    print(BASE / "summary.md", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
