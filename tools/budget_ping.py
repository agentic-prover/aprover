#!/usr/bin/env python3
"""Exit 0 if the LLM API answers a 1-token call; exit 1 on budget/400/any failure.
Used by the overnight orchestrator to skip agentic runs when the workspace API
budget is exhausted (avoids the silent-fallback-to-confirmed contamination)."""
import sys
try:
    from bmc_agent.config import Config
    from bmc_agent.llm import LLMClient
    c = Config(); c.llm_provider = "anthropic"; c.llm_model = "claude-sonnet-4-6"
    LLMClient(c).complete("reply ok", "ping", max_tokens=5)
    print("BUDGET_OK"); sys.exit(0)
except Exception as e:
    print("BUDGET_FAIL:", str(e)[:120]); sys.exit(1)
