"""
Configuration dataclass for GRACE.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """Global configuration for a GRACE verification run."""

    # LLM settings
    llm_model: str = "claude-sonnet-4-6"
    llm_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    llm_base_url: str = ""  # optional OpenRouter or proxy base URL

    # CBMC settings
    cbmc_path: str = "cbmc"
    cbmc_unwind: int = 4
    cbmc_timeout: int = 120  # seconds

    # Artifact settings
    artifact_dir: str = "artifacts"

    # Refinement loop settings
    max_spec_retries: int = 3
    max_refinement_iters: int = 5

    # Batch processing
    batch_size: int = 10

    # V2 features
    enable_dual_spec: bool = True    # generate spec twice with different emphases, flag disagreements
    enable_spec_quality: bool = False  # run Phase 5 spec quality analysis (expensive)

    # V3 features
    skip_refinement: bool = False    # filtering-only ablation: classify spurious but skip spec update + caller requeue

    def resolved_api_key(self) -> str:
        """Return the effective API key, reading from env if not set directly."""
        if self.llm_api_key:
            return self.llm_api_key
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        return key

    @classmethod
    def from_env(cls) -> "Config":
        """Create a Config populated from environment variables where available."""
        return cls(
            llm_model=os.environ.get("AMC_LLM_MODEL", "claude-sonnet-4-6"),
            llm_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            llm_base_url=os.environ.get("AMC_LLM_BASE_URL", ""),
            cbmc_path=os.environ.get("AMC_CBMC_PATH", "cbmc"),
            cbmc_unwind=int(os.environ.get("AMC_CBMC_UNWIND", "4")),
            cbmc_timeout=int(os.environ.get("AMC_CBMC_TIMEOUT", "120")),
            artifact_dir=os.environ.get("AMC_ARTIFACT_DIR", "artifacts"),
            max_spec_retries=int(os.environ.get("AMC_MAX_SPEC_RETRIES", "3")),
            max_refinement_iters=int(os.environ.get("AMC_MAX_REFINEMENT_ITERS", "5")),
            batch_size=int(os.environ.get("AMC_BATCH_SIZE", "10")),
            enable_dual_spec=os.environ.get("AMC_ENABLE_DUAL_SPEC", "true").lower() != "false",
            enable_spec_quality=os.environ.get("AMC_ENABLE_SPEC_QUALITY", "false").lower() == "true",
            skip_refinement=os.environ.get("AMC_SKIP_REFINEMENT", "false").lower() == "true",
        )
