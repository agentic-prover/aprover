#!/usr/bin/env bash
# OPT-IN auto-verify hook: after Claude Code edits a .c file, nudge it to run
# bmc_verify on the change. Reads the PostToolUse JSON on stdin; emits a
# systemMessage only for C files. Kept lightweight (no verification here -- it
# just prompts Claude to call the MCP tool, so cost stays under the model's control).
payload="$(cat)"
f="$(printf '%s' "$payload" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("tool_input") or {}).get("file_path",""))' 2>/dev/null)"
case "$f" in
  *.c|*.h)
    printf '{"systemMessage":"bmc-agent: %s changed — consider `bmc_verify(file=\\"%s\\")` to memory-safety-check it."}\n' "$f" "$f"
    ;;
  *) : ;;
esac
