#!/usr/bin/env python3
"""bmc-agent MCP server for Claude Code.

Exposes CBMC-backed bounded-model-checking verification as MCP tools so a Claude
Code session can verify C code it is reading/writing and act on the findings.

Design:
  * Runs bmc-agent in CLAUDE-CODE (subscription) mode by default: the internal
    spec-gen/triage LLM calls go through the user's logged-in `claude` CLI
    subscription -- NO separate ANTHROPIC_API_KEY needed. (ANTHROPIC_API_KEY is
    stripped from the child env so the CLI uses the subscription, not the API.)
  * Never sets `ulimit -v` (Node/V8 in the `claude` CLI reserves huge virtual
    memory; a vmem cap crashes it).
  * Pins the model to one the subscription serves (claude-sonnet-4-6).

Requires: CBMC on PATH, the `claude` CLI logged in (`claude /login`), and the
bmc-agent package importable (BMC_AGENT_ROOT or auto-derived from this file).
"""
from __future__ import annotations
import json, os, re, subprocess, sys, tempfile, shutil

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - dependency hint
    sys.stderr.write("bmc-agent MCP server needs the MCP SDK: pip install mcp\n")
    raise

BMC_ROOT = os.environ.get("BMC_AGENT_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CC_MODEL = os.environ.get("BMC_AGENT_CC_MODEL", "claude-sonnet-4-6")
DEFAULT_TIMEOUT = int(os.environ.get("BMC_AGENT_VERIFY_TIMEOUT", "600"))

mcp = FastMCP("bmc-agent")

CONF_TIERS = {"confirmed_bmc", "confirmed_dynamic", "confirmed_system_entry"}


def _cbmc_ok() -> str:
    if shutil.which("cbmc") is None:
        return "CBMC not found on PATH. Install it (apt install cbmc / brew install cbmc)."
    return ""


def _parse_reports(outdir: str, driver: str) -> dict:
    """Read bmc-agent's per-function bug_report.json / latent_report.json."""
    confirmed, latent = [], []
    root = os.path.join(outdir, driver)
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn not in ("bug_report.json", "latent_report.json"):
                continue
            try:
                r = json.load(open(os.path.join(dirpath, fn))).get("report", {})
            except Exception:
                continue
            item = {
                "function": r.get("function_name"),
                "property": r.get("violated_property"),
                "bug_type": r.get("bug_type"),
                "confidence": r.get("confidence"),
                "call_chain": r.get("call_chain") or [],
                "reproducer": (r.get("reproducer") or "")[:1200] or None,
                "triage": r.get("triage"),
            }
            if fn == "latent_report.json":
                latent.append(item)
            elif (r.get("confidence") or "") in CONF_TIERS:
                confirmed.append(item)
    return {"confirmed": confirmed, "latent": latent}


@mcp.tool()
def bmc_verify(file: str, function: str = "", goal: str = "memsafety") -> dict:
    """Verify a C source file with bmc-agent (CBMC-backed bounded model checking).

    Args:
        file: absolute path to a .c/.h source file to verify.
        function: optional -- restrict the reported findings to this function.
        goal: what to verify for -- "memsafety" (bounds+pointer; default, low
              false-alarm), "overflow" (integer overflow/conversion), "reach"
              (reachability of asserts/reach_error), or "all" (every check;
              higher false-alarm rate). Spec/postcondition asserts are always
              checked regardless.

    Returns a dict: {ok, verdict, confirmed_bugs:[...], latent:[...], summary,
    error?}. Each confirmed bug carries the violated property, confidence tier,
    call chain, a C reproducer (when available), and an advisory triage note.
    """
    err = _cbmc_ok()
    if err:
        return {"ok": False, "error": err}
    if not os.path.isfile(file):
        return {"ok": False, "error": f"file not found: {file}"}
    if goal not in ("memsafety", "overflow", "reach", "all"):
        return {"ok": False, "error": f"invalid goal '{goal}' (memsafety|overflow|reach|all)"}

    driver = "mcp_" + re.sub(r"[^A-Za-z0-9_]", "_", os.path.basename(file))
    outdir = tempfile.mkdtemp(prefix="bmc_mcp_")
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # force subscription (claude-code), not API
    for k in ("OPENROUTER_API_KEY", "K2THINK_API_KEY"):
        env.pop(k, None)
    env["BMC_AGENT_LLM_MODEL"] = CC_MODEL  # pin a subscription-served model
    cmd = [sys.executable, "-m", "bmc_agent.cli", "verify",
           "--source", file, "--driver", driver,
           "--agentic-claude-code", "--plan", "--goal", goal,
           "--model", CC_MODEL, "--output", outdir]
    try:
        # No ulimit -v: the claude CLI (Node/V8) needs unbounded virtual memory.
        proc = subprocess.run(cmd, cwd=BMC_ROOT, env=env, capture_output=True,
                              text=True, timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        shutil.rmtree(outdir, ignore_errors=True)
        return {"ok": False, "error": f"verification timed out after {DEFAULT_TIMEOUT}s "
                f"(raise BMC_AGENT_VERIFY_TIMEOUT for large files)"}
    out = proc.stdout + "\n" + proc.stderr
    if "Invalid API key" in out or "please run /login" in out.lower():
        shutil.rmtree(outdir, ignore_errors=True)
        return {"ok": False, "error": "the `claude` CLI is not logged in -- run `claude /login` "
                "(subscription) so bmc-agent can use it for its internal LLM calls."}
    reports = _parse_reports(outdir, driver)
    if function:
        reports["confirmed"] = [b for b in reports["confirmed"] if b.get("function") == function]
        reports["latent"] = [b for b in reports["latent"] if b.get("function") == function]
    m = re.search(r"AMC Pipeline END: (\d+) real bug\(s\), (\d+) latent", out)
    n_real = len(reports["confirmed"])
    shutil.rmtree(outdir, ignore_errors=True)
    verdict = "BUGS_FOUND" if n_real else ("LATENT_ONLY" if reports["latent"] else "CLEAN")
    return {
        "ok": True,
        "verdict": verdict,
        "goal": goal,
        "confirmed_bugs": reports["confirmed"],
        "latent": reports["latent"],
        "summary": (f"{n_real} confirmed bug(s), {len(reports['latent'])} latent "
                    f"(goal={goal}). Spec/postcondition asserts always checked."),
    }


if __name__ == "__main__":
    mcp.run()
