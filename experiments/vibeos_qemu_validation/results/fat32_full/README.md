# VibeOS QEMU validation results

This directory contains the consolidated post-hoc QEMU validation result for
the VibeOS finding set.

- Source artifact: `artifacts/vibeos_posthoc_qemu_fat32_full_20260601_180717`
- Exception retry artifact: `artifacts/vibeos_posthoc_qemu_fat32_retry_exceptions_20260601_185349`
- Result set: 60 existing VibeOS findings
- Replay environment: QEMU target replay with generated FAT32 disk and font resource
- Model for generated replay plans: `anthropic/claude-sonnet-4.6` via OpenRouter

The committed files are:

- `summary.md`: human-readable consolidated summary
- `summary.json`: aggregate counts
- `results.jsonl`: one row per finding after replacing the original three
  replay exceptions with targeted retry results

The full QEMU logs, generated replay diffs, LLM prompts, and temporary
worktrees remain under `artifacts/` and are intentionally not committed here.
They are useful for local audit but too noisy for the code branch.
