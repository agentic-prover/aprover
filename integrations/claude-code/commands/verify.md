---
description: Verify C code for memory-safety bugs with bmc-agent (CBMC-backed)
argument-hint: [file] [function] [goal: memsafety|overflow|all]
---
Verify the user's C code with the `bmc_verify` MCP tool (bmc-agent, CBMC-backed).

From the user's request `$ARGUMENTS`, determine the target file (default: the C
file currently in focus / most recently edited) and, if named, the function and
goal (default goal: `memsafety`).

Call `bmc_verify(file=..., function=..., goal=...)`. Then:
- If `verdict` is `BUGS_FOUND`: for each confirmed bug, explain the violated
  property and call chain, show the reproducer if present, and note the advisory
  triage verdict. Propose a concrete fix. Treat the SOLVER verdict as ground
  truth; treat `triage` as advisory (a `likely_fp` triage note means review, not
  ignore).
- If `LATENT_ONLY`: report the latent findings as "reachable via a future/adversarial
  caller but no in-tree caller triggers them" and let the user decide.
- If `CLEAN`: state it verified clean for the chosen goal, and remind that
  `memsafety` does not check integer overflow (offer `goal=overflow`/`all`).

If the tool returns an error about the `claude` CLI not being logged in, tell the
user to run `claude /login` (bmc-agent uses their Claude subscription for its
internal LLM calls). If CBMC is missing, tell them to install it.
