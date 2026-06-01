#!/usr/bin/env python3
"""Overnight --agentic test on OSS-Fuzz parser targets (detached, session-surviving).

Two-stage coaudit loop per target, methodologically aligned with /coaudit:

  Stage 1 (cheap net, fast model): run oss_bmc.py's FP-hint net to SELECT which
           functions are worth the expensive agentic pass. Uses the configured
           OpenAI/gpt-4o-mini env (~/.config/bmc-agent/env).
  Stage 2 (agentic adjudication, HYBRID): re-run JUST those flagged functions
           through `oss_bmc.py --check-functions --cross-file --agentic`, keeping
           the env so it's claude-code + API agents:
             - spec_gen -> fast API (gpt-4o-mini, pinned by BMC_AGENT_LLM_SPEC_GEN_*)
             - refinement/soundness, realism, classifier, dynamic_repro, dynval_triage,
               harness-repair -> claude-code (caller-grounded roles; no API key needed).
           Claude-code uses the local subscription, so the agent half survives the
           night with nothing to expire; only spec_gen depends on the API key.

Robustness (this runs unattended until the user is back):
  * every subprocess has a hard timeout — nothing hangs the night;
  * every target is wrapped in try/except — one failure never kills the loop;
  * a wall-clock DEADLINE bounds the whole run (default 11h);
  * results stream to <out>/SUMMARY.md (human) + <out>/results.jsonl (machine)
    after every target, so partial results are always checkable.

Usage (detached, survives logout):
  nohup python3 tools/run_agentic_oss_fuzz_overnight.py --hours 11 \
        > /tmp/agentic_oss_fuzz_overnight.log 2>&1 &

Only ASan-confirmed bugs are real bugs (coaudit rule); the agentic verdicts here
are LEADS — verified=False / UNRESOLVED / REAL_BUG labels to adjudicate when back.
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
# Build-unready targets are logged and skipped, never silently dropped.
DEFAULT_TARGETS = [
    "libredwg",    # known productive (fix-stream outpaces fuzzing)
    "libxmp",      # tracker-module parser
    "libmodplug",  # tracker-module parser (historically OOB-prone)
    "faad2",       # AAC decoder
    "matio",       # MAT-file parser
    "openjpeg",    # JPEG2000 codec
    "libsndfile",  # audio container parser
    "gpac",        # MP4/box demuxer
]

# net (stage 1) caps
NET_MAX_FILES = 2
NET_PER_FILE_TIMEOUT = 480          # bmc-agent per-file verify cap (passed to oss_bmc)
NET_WALL_TIMEOUT = 2400             # whole stage-1 subprocess cap
# agentic (stage 2) caps
AGENTIC_PER_FUNC_TIMEOUT = 1500     # passed to oss_bmc --timeout (per fn/file)
AGENTIC_WALL_TIMEOUT = 6000         # whole stage-2 subprocess cap
MAX_FUNCS_PER_TARGET = 6            # cap the agentic shortlist


def log(msg: str) -> None:
    print(f"[overnight {time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env_file() -> dict:
    """Parse `export K=V` lines from the bmc-agent env file (stage-1 model)."""
    out = {}
    if not ENVF.is_file():
        return out
    for line in ENVF.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
        if m and not line.startswith("#"):
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def net_env() -> dict:
    """Stage-1 env: host env + the configured fast model (gpt-4o-mini)."""
    env = os.environ.copy()
    env.update(load_env_file())
    return env


def agentic_env() -> dict:
    """Stage-2 env: HYBRID. Keep the env (so spec_gen stays pinned to the fast
    API via BMC_AGENT_LLM_SPEC_GEN_*) and let --agentic route every OTHER agent
    role (refinement/soundness, realism, classifier, dynamic_repro, dynval_triage,
    harness-repair) to claude-code — those are the caller-grounded roles that
    earn the agent. Verified resolved routing: spec_gen->openai, rest->claude-code.
    Claude-code needs no API key (subscription); spec_gen's gpt-4o-mini key is in
    the env. So: claude-code + API agents, exactly as intended."""
    return net_env()


def run(cmd: list[str], env: dict, logpath: Path, timeout: int) -> int:
    """Run a subprocess to a log file with a hard timeout. Returns rc (124 = timeout)."""
    logpath.parent.mkdir(parents=True, exist_ok=True)
    with logpath.open("w") as fh:
        fh.write(f"$ {' '.join(cmd)}\n\n")
        fh.flush()
        try:
            p = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                               timeout=timeout)
            return p.returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[overnight] TIMEOUT after {timeout}s\n")
            return 124
        except Exception as exc:  # never let a target kill the loop
            fh.write(f"\n[overnight] EXCEPTION: {exc!r}\n")
            return 1


def flagged_functions(net_out: Path) -> list[str]:
    """Extract the memory-safety FP-hint function names from net artifacts —
    same filter as oss_bmc.emit_hints (pointer/bounds/overflow, skip .unwind.)."""
    funcs: list[str] = []
    seen = set()
    for br in net_out.rglob("bug_report.json"):
        try:
            rep = json.loads(br.read_text()).get("report", {})
        except Exception:
            continue
        for c in (rep.get("counterexamples") or []):
            fp = (c.get("failing_property") or "")
            if ".unwind." in fp:
                continue
            if not any(k in fp for k in ("pointer", "bounds", "overflow")):
                continue
            fn = rep.get("function_name")
            if fn and fn not in seen:
                seen.add(fn)
                funcs.append(fn)
    return funcs[:MAX_FUNCS_PER_TARGET]


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
    return out[:60]


def process_target(t: str, out_root: Path, summary: Path, results: Path) -> None:
    log(f"=== target: {t} ===")
    tdir = out_root / t
    proj = CORPORA / t
    if not proj.is_dir():
        log(f"  SKIP {t}: not cloned at {proj}")
        _record(summary, results, t, status="skip_not_cloned", funcs=[], verdicts=[])
        return

    # Stage 0/1: build-readiness + cheap FP-hint net (fast model).
    net_out = Path(f"/tmp/oss_bmc/{t}")
    rc = run(
        ["python3", str(OSS_BMC), t, "--max-files", str(NET_MAX_FILES),
         "--timeout", str(NET_PER_FILE_TIMEOUT), "--out", str(net_out)],
        env=net_env(), logpath=tdir / "01_net.log", timeout=NET_WALL_TIMEOUT,
    )
    if rc != 0:
        log(f"  net stage rc={rc} (continuing — may still have partial hints)")

    funcs = flagged_functions(net_out)
    if not funcs:
        log(f"  no memory-safety candidates from net — nothing to adjudicate")
        _record(summary, results, t, status="no_candidates", funcs=[], verdicts=[])
        return
    log(f"  {len(funcs)} candidate fn(s) -> agentic: {', '.join(funcs)}")

    # Stage 2: agentic adjudication of JUST those functions (claude-code, sanitized env).
    alog = tdir / "02_agentic.log"
    rc2 = run(
        ["python3", str(OSS_BMC), t, "--check-functions", ",".join(funcs),
         "--cross-file", "--agentic", "--timeout", str(AGENTIC_PER_FUNC_TIMEOUT),
         "--out", str(net_out)],
        env=agentic_env(), logpath=alog, timeout=AGENTIC_WALL_TIMEOUT,
    )
    verdicts = harvest_verdicts(alog)
    log(f"  agentic rc={rc2}; {len(verdicts)} verdict line(s) harvested")
    _record(summary, results, t, status=f"done(rc={rc2})", funcs=funcs, verdicts=verdicts)


def _record(summary: Path, results: Path, t: str, *, status: str,
            funcs: list[str], verdicts: list[str]) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with summary.open("a") as fh:
        fh.write(f"\n## {t} — {status} ({ts})\n")
        if funcs:
            fh.write(f"- agentic shortlist: {', '.join(funcs)}\n")
        if verdicts:
            fh.write("- agentic verdict lines:\n")
            for v in verdicts:
                fh.write(f"    - {v}\n")
        else:
            fh.write("- (no lead verdicts harvested)\n")
    with results.open("a") as fh:
        fh.write(json.dumps({"ts": ts, "target": t, "status": status,
                             "funcs": funcs, "verdicts": verdicts}) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=11.0, help="wall-clock deadline")
    ap.add_argument("--targets", default=None, help="comma-separated override list")
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
        f"# Overnight --agentic OSS-Fuzz run\n\n"
        f"- started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- deadline: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deadline))} "
        f"({args.hours}h)\n"
        f"- targets: {', '.join(targets)}\n"
        f"- engine: stage1 cheap net (gpt-4o-mini) -> stage2 --agentic (claude-code)\n"
        f"- NOTE: verdicts are LEADS; only ASan-confirmed bugs are real bugs.\n"
    )
    log(f"out={out_root}  deadline={args.hours}h  targets={targets}")

    rnd = 0
    while time.time() < deadline:
        rnd += 1
        log(f"--- round {rnd} ---")
        for t in targets:
            if time.time() >= deadline:
                log("deadline reached — stopping")
                break
            try:
                process_target(t, out_root, summary, results)
            except Exception as exc:  # absolute backstop
                log(f"  target {t} raised {exc!r} — continuing")
        else:
            # finished a full round with time left; loop again (re-runs may
            # deepen as specs are cached). Avoid a tight spin if everything
            # no-op'd instantly.
            time.sleep(5)
            continue
        break

    with summary.open("a") as fh:
        fh.write(f"\n---\n_run ended {time.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"after {rnd} round(s)._\n")
    log(f"DONE after {rnd} round(s). Results: {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
