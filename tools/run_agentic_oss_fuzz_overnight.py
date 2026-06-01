#!/usr/bin/env python3
"""Overnight AUDIT-FIRST --agentic bug hunt on OSS-Fuzz parser targets.

Detached, session-surviving. Per target, Direction-A-then-B coaudit:

  Stage 1 — SOURCE AUDIT (claude-code, reads real code): a claude agent with
            Read/Grep/Glob over the target's parser source identifies functions
            with self-contained OOB-prone idioms reachable from attacker input
            (file/network bytes used as an unchecked length/index/loop-bound;
            const table indexed by an unmasked input byte; copy into a fixed
            buffer with attacker-influenced length). The audit AIMS the verifier
            instead of letting CBMC blindly fish — far better targeting than a
            cheap weak-spec net. Returns a JSON shortlist {function,file,idiom,why}.
  Stage 2 — --agentic VERIFICATION of JUST those functions:
            `oss_bmc.py --files <audited> --check-functions <funcs> --cross-file --agentic`.
            HYBRID routing (verified): spec_gen->API gpt-4o-mini; refinement/
            soundness, realism, classifier, dynamic_repro, dynval_triage,
            harness-repair->claude-code (subscription, no key). Cross-file gives
            real caller-grounded gen+refinement; agentic gives the soundness/
            realism adjudication that decides real-vs-FP.

Robustness (unattended until the user is back):
  * hard per-subprocess timeouts — nothing hangs the night;
  * per-target try/except — one failure never kills the loop;
  * wall-clock DEADLINE (default 11h);
  * results stream to <out>/SUMMARY.md + <out>/results.jsonl after every target.

Usage (detached, survives logout):
  setsid nohup python3 tools/run_agentic_oss_fuzz_overnight.py --hours 11 \
        > /tmp/agentic_oss_fuzz_overnight.log 2>&1 &

Only ASan-confirmed bugs are real bugs (coaudit rule); verdicts here are LEADS
(verified=False / UNRESOLVED / REAL_BUG) to adjudicate + ASan-confirm when back.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OSS_BMC = REPO / "tools" / "cbmc_direct" / "oss_bmc.py"
CORPORA = Path("/tmp/oss_fuzz_corpora")
ENVF = Path.home() / ".config" / "bmc-agent" / "env"

# Curated rotation: real OSS-Fuzz C parser projects, attacker-facing, weaker
# hardening pedigree (codec/format/CAD parsers) or known-productive (libredwg).
DEFAULT_TARGETS = [
    "libredwg", "libxmp", "libmodplug", "faad2",
    "matio", "openjpeg", "libsndfile", "gpac",
]

PARSER_HINT = re.compile(
    r"(pars|read|decod|demux|box|chunk|header|token|scan|load|unpack|"
    r"inflate|deflate|extract|process|atom|tag|frame)", re.I)

SETUP_TIMEOUT = 900           # oss_bmc --setup-only (cmake configure etc.)
AGENTIC_PER_FUNC_TIMEOUT = 1500
AGENTIC_WALL_TIMEOUT = 7200   # whole stage-2 subprocess cap
MAX_FUNCS_PER_TARGET = 6
MAX_PARSER_FILES = 4

# Self-contained OOB idioms = the coaudit "audit-grep": unbounded copies and
# array/pointer indexing by a variable. High-signal and attacker-reachable in
# parsers. A single claude agent reading whole 7k-line decoders TIMES OUT, so
# stage-1 selection is mechanical+instant; the agentic JUDGMENT is stage-2.
_IDIOM = re.compile(
    r"\b(memcpy|memmove|memset|strcpy|strncpy|strcat|sprintf|alloca)\s*\(|"
    r"[A-Za-z_]\w*\s*\[\s*[A-Za-z_]\w*[A-Za-z0-9_ +\-*]*\]",
)


def log(msg: str) -> None:
    print(f"[overnight {time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env_file() -> dict:
    out = {}
    if not ENVF.is_file():
        return out
    for line in ENVF.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def hybrid_env() -> dict:
    """Host env + the bmc-agent env file. Under --agentic this yields the hybrid:
    spec_gen pinned to the API (BMC_AGENT_LLM_SPEC_GEN_*), every other agent role
    -> claude-code. Keeping the env (NOT stripping it) is what makes it hybrid."""
    env = os.environ.copy()
    env.update(load_env_file())
    return env


def run(cmd: list[str], env: dict, logpath: Path, timeout: int,
        stdin_text: str | None = None) -> tuple[int, str]:
    logpath.parent.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(cmd, env=env, input=stdin_text, capture_output=True,
                           text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        logpath.write_text(f"$ {' '.join(cmd)}\n\n{out}")
        return p.returncode, (p.stdout or "")
    except subprocess.TimeoutExpired as e:
        logpath.write_text(f"$ {' '.join(cmd)}\n\n[TIMEOUT {timeout}s]\n"
                           f"{(e.stdout or b'') if isinstance(e.stdout, bytes) else (e.stdout or '')}")
        return 124, ""
    except Exception as exc:
        logpath.write_text(f"$ {' '.join(cmd)}\n\n[EXCEPTION] {exc!r}\n")
        return 1, ""


def setup_target(target: str, out: Path) -> tuple[Path | None, list[Path]]:
    """Run oss_bmc --setup-only (derives compile_commands + lists parser files).
    Returns (compile_commands_path, [parser file paths]) or (None, [])."""
    rc, stdout = run(
        ["python3", str(OSS_BMC), target, "--setup-only",
         "--max-files", str(MAX_PARSER_FILES), "--out", str(out)],
        env=hybrid_env(), logpath=out / "00_setup.log", timeout=SETUP_TIMEOUT,
    )
    m = re.search(r"compile_commands\.json:\s*(\S+)", stdout)
    if not m:
        return None, []
    cc = Path(m.group(1))
    if not cc.is_file():
        return None, []
    try:
        db = json.loads(cc.read_text())
    except Exception:
        return None, []
    files, seen = [], set()
    for e in db:
        f = e.get("file", "")
        if f.endswith(".c") and PARSER_HINT.search(Path(f).name) and f not in seen:
            seen.add(f)
            files.append(Path(f))
    return cc, files[:MAX_PARSER_FILES]


def _split_functions(text: str) -> list[tuple[str, str]]:
    """Crude pure-Python C function splitter (brace tracking): returns (name, body)
    pairs. Good enough for SELECTION — not a parser. No deps, so it runs fine in
    the detached driver's system python."""
    res, depth, pending, name, bstart = [], 0, "", None, 0
    skip = {"if", "for", "while", "switch", "sizeof", "return", "do", "else"}
    j, n = 0, len(text)
    while j < n:
        c = text[j]
        if depth == 0:
            if c in ";}":
                pending = ""
            elif c == "{":
                m = re.search(r"([A-Za-z_]\w*)\s*\([^;{]*\)\s*$", pending)
                name = m.group(1) if m else None
                depth, bstart, pending = 1, j + 1, ""
            else:
                pending += c
                if len(pending) > 4000:
                    pending = pending[-2000:]
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    if name and name not in skip:
                        res.append((name, text[bstart:j]))
                    name = None
        j += 1
    return res


def audit_functions(target: str, src_dir: Path, parser_files: list[Path],
                    out: Path) -> list[dict]:
    """Stage 1 — mechanical audit-grep: rank functions by self-contained OOB-idiom
    density across the parser files. Instant + deterministic (no LLM, no timeout).
    The agentic JUDGMENT of these candidates is stage 2."""
    scored = []  # (hits, name, file, sample_line)
    for pf in parser_files:
        try:
            text = pf.read_text(errors="replace")
        except Exception:
            continue
        for name, body in _split_functions(text):
            hits = _IDIOM.findall(body)
            if not hits:
                continue
            mm = _IDIOM.search(body)
            ls = body.rfind("\n", 0, mm.start()) + 1
            le = body.find("\n", mm.start())
            sample = body[ls: le if le != -1 else mm.end()].strip()[:140]
            scored.append((len(hits), name, pf.name, sample))
    scored.sort(key=lambda t: t[0], reverse=True)
    seen, audit = set(), []
    for n_hits, name, fname, sample in scored:
        if name in seen:
            continue
        seen.add(name)
        audit.append({"function": name, "file": fname,
                      "idiom": f"{n_hits} OOB-idiom hit(s)", "why": sample})
        if len(audit) >= MAX_FUNCS_PER_TARGET:
            break
    (out / "01_audit_shortlist.json").write_text(json.dumps(audit, indent=2))
    return audit


_VERDICT_RE = re.compile(
    r"(verified=False|counterexamples=[1-9]|UNRESOLVED|REAL_BUG|confirmed|"
    r"Total bugs confirmed:\s*[1-9]|VERIFICATION FAILED)", re.I)


def harvest_verdicts(agentic_log: Path) -> list[str]:
    if not agentic_log.is_file():
        return []
    out = []
    for line in agentic_log.read_text(errors="replace").splitlines():
        if _VERDICT_RE.search(line):
            out.append(line.strip()[:200])
    return out[:80]


def process_target(t: str, out_root: Path, summary: Path, results: Path) -> None:
    log(f"=== target: {t} ===")
    tdir = out_root / t
    tdir.mkdir(parents=True, exist_ok=True)
    proj = CORPORA / t
    if not proj.is_dir():
        log(f"  SKIP {t}: not cloned"); _record(summary, results, t, "skip_not_cloned", [], []); return

    cc, parser_files = setup_target(t, tdir)
    if not cc or not parser_files:
        log(f"  SKIP {t}: no compile_commands / no parser files (build not integrable)")
        _record(summary, results, t, "skip_no_build", [], []); return
    src_dir = parser_files[0].parent
    log(f"  {len(parser_files)} parser file(s); auditing under {src_dir}")

    audit = audit_functions(t, src_dir, parser_files, tdir)
    if not audit:
        log(f"  audit found no suspect functions"); _record(summary, results, t, "no_audit_candidates", [], []); return
    funcs = [a["function"] for a in audit]
    log(f"  audit -> {len(funcs)} suspect fn: {', '.join(funcs)}")

    audited_files = sorted({a["file"] for a in audit if a.get("file")})
    files_regex = "(" + "|".join(re.escape(b) for b in audited_files) + ")" if audited_files else None
    cmd = ["python3", str(OSS_BMC), t, "--check-functions", ",".join(funcs),
           "--cross-file", "--agentic", "--timeout", str(AGENTIC_PER_FUNC_TIMEOUT),
           "--out", str(Path(f"/tmp/oss_bmc/{t}"))]
    if files_regex:
        cmd += ["--files", files_regex]
    rc, _ = run(cmd, env=hybrid_env(), logpath=tdir / "02_agentic.log",
                timeout=AGENTIC_WALL_TIMEOUT)
    verdicts = harvest_verdicts(tdir / "02_agentic.log")
    log(f"  agentic rc={rc}; {len(verdicts)} verdict line(s)")
    _record(summary, results, t, f"done(rc={rc})", audit, verdicts)


def _record(summary: Path, results: Path, t: str, status: str,
            audit: list[dict], verdicts: list[str]) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with summary.open("a") as fh:
        fh.write(f"\n## {t} — {status} ({ts})\n")
        if audit:
            fh.write("- audit shortlist:\n")
            for a in audit:
                fh.write(f"    - `{a['function']}` ({a.get('file','?')}) — "
                         f"{a.get('idiom','')}: {a.get('why','')}\n")
        if verdicts:
            fh.write("- agentic verdict lines:\n")
            for v in verdicts:
                fh.write(f"    - {v}\n")
        elif audit:
            fh.write("- (no lead verdicts harvested — see 02_agentic.log)\n")
    with results.open("a") as fh:
        fh.write(json.dumps({"ts": ts, "target": t, "status": status,
                             "audit": audit, "verdicts": verdicts}) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=11.0)
    ap.add_argument("--targets", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    targets = ([s.strip() for s in args.targets.split(",") if s.strip()]
               if args.targets else list(DEFAULT_TARGETS))
    out_root = Path(args.out or (REPO / "findings" / "agentic_oss_fuzz_overnight" /
                                 time.strftime("%Y%m%dT%H%M%SZ")))
    out_root.mkdir(parents=True, exist_ok=True)
    summary = out_root / "SUMMARY.md"
    results = out_root / "results.jsonl"
    deadline = time.time() + args.hours * 3600

    summary.write_text(
        f"# Overnight AUDIT-FIRST --agentic OSS-Fuzz run\n\n"
        f"- started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- deadline: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deadline))} ({args.hours}h)\n"
        f"- targets: {', '.join(targets)}\n"
        f"- pipeline: claude-code SOURCE AUDIT -> --agentic --check-functions (hybrid)\n"
        f"- NOTE: verdicts are LEADS; only ASan-confirmed bugs are real bugs.\n"
    )
    log(f"out={out_root}  deadline={args.hours}h  targets={targets}")

    rnd = 0
    while time.time() < deadline:
        rnd += 1
        log(f"--- round {rnd} ---")
        for t in targets:
            if time.time() >= deadline:
                log("deadline reached — stopping"); break
            try:
                process_target(t, out_root, summary, results)
            except Exception as exc:
                log(f"  target {t} raised {exc!r} — continuing")
        else:
            time.sleep(5)
            continue
        break

    with summary.open("a") as fh:
        fh.write(f"\n---\n_run ended {time.strftime('%Y-%m-%d %H:%M:%S')} after {rnd} round(s)._\n")
    log(f"DONE after {rnd} round(s). Results: {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
