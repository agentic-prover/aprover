# bmc-agent for Claude Code

Verify C code for memory-safety (and overflow / reachability) bugs from inside a
Claude Code session, using CBMC-backed bounded model checking. Claude Code calls
the `bmc_verify` tool on the function/file you're working on, gets structured
findings (counterexamples, confidence, advisory triage), and helps you fix them.

## What you get
- MCP tool **`bmc_verify(file, function, goal)`** — `goal`: `memsafety` (default,
  low false-alarm), `overflow`, `reach`, or `all`.
- Slash command **`/verify`** — e.g. `/verify http.c parse_header`.
- Optional **auto-verify hook** — nudges a verify after Claude Code edits a `.c` file.

## Requirements
1. **CBMC** on PATH — `apt install cbmc` (Linux) or `brew install cbmc` (macOS).
2. The **`claude` CLI logged in** — `claude /login`. bmc-agent runs its internal
   spec-gen/triage LLM through your Claude **subscription** (no separate API key,
   flat-rate). The MCP server strips `ANTHROPIC_API_KEY` from its child env so the
   subscription is used, and pins a subscription-served model.
3. Python deps: `pip install mcp`, and bmc-agent's own requirements.

## Install
Add the plugin (or point Claude Code's MCP config at `bmc_mcp_server.py`). The
bundled `.mcp.json` registers the `bmc-agent` server; `BMC_AGENT_ROOT` points at
the bmc-agent checkout.

## Notes
- Do **not** run the MCP server under a `ulimit -v` cap — the `claude` CLI (Node)
  needs unbounded virtual memory.
- The solver verdict is ground truth; `triage` is advisory. `memsafety` does not
  check integer overflow — use `goal=overflow`/`all` for that.
- Large files: raise `BMC_AGENT_VERIFY_TIMEOUT` (default 600s).
