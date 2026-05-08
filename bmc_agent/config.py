"""
Configuration dataclass for BMC-Agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """Global configuration for a BMC-Agent verification run."""

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

    # Multi-file / whole-codebase support
    include_dirs: list = field(default_factory=list)  # -I paths for cc -E
    cc_path: str = "cc"                               # C compiler for preprocessing
    preprocess: bool = False                          # run cc -E before parsing

    # V2 features
    enable_dual_spec: bool = True    # generate spec twice with different emphases, flag disagreements
    enable_spec_quality: bool = False  # run Phase 5 spec quality analysis (expensive)

    # V3 features
    skip_refinement: bool = False    # filtering-only ablation: classify spurious but skip spec update + caller requeue
    max_requeue_per_function: int = 3  # global cap on how many times a single function can be re-queued

    # Dynamic validation settings (Phase 3 Stage 3)
    enable_dynamic_validation: bool = False  # compile and run a GCC harness to confirm real faults
    dynamic_validation_timeout: int = 30     # seconds to allow the compiled harness to run
    dynamic_cc_path: str = "gcc"             # C compiler for dynamic harness compilation

    # Flag selector settings (Phase 1.5: per-function CBMC flag selection)
    enable_flag_selection: bool = False      # LLM selects per-function CBMC flags (e.g. --unsigned-overflow-check)

    # Realism checker settings (Phase 3 post-validation LLM audit)
    enable_realism_check: bool = False       # LLM agent that audits REAL_BUG findings for realistic exploitability
    enable_realism_thinking: bool = False    # use extended thinking in the realism checker (slower, higher quality)

    # Threat model — shapes CBMC baseline flags, spec prompts, and realism context.
    # "security"   (default): memory safety + integer overflow, attacker-controlled inputs.
    # "safety"     : functional correctness + no-crash, valid system state.
    # "functional" : spec correctness only, no extra CBMC checks.
    threat_model: str = "security"

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
            llm_model=os.environ.get("BMC_AGENT_LLM_MODEL", "claude-sonnet-4-6"),
            llm_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            llm_base_url=os.environ.get("BMC_AGENT_LLM_BASE_URL", ""),
            cbmc_path=os.environ.get("BMC_AGENT_CBMC_PATH", "cbmc"),
            cbmc_unwind=int(os.environ.get("BMC_AGENT_CBMC_UNWIND", "4")),
            cbmc_timeout=int(os.environ.get("BMC_AGENT_CBMC_TIMEOUT", "120")),
            artifact_dir=os.environ.get("BMC_AGENT_ARTIFACT_DIR", "artifacts"),
            max_spec_retries=int(os.environ.get("BMC_AGENT_MAX_SPEC_RETRIES", "3")),
            max_refinement_iters=int(os.environ.get("BMC_AGENT_MAX_REFINEMENT_ITERS", "5")),
            batch_size=int(os.environ.get("BMC_AGENT_BATCH_SIZE", "10")),
            enable_dual_spec=os.environ.get("BMC_AGENT_ENABLE_DUAL_SPEC", "true").lower() != "false",
            enable_spec_quality=os.environ.get("BMC_AGENT_ENABLE_SPEC_QUALITY", "false").lower() == "true",
            skip_refinement=os.environ.get("BMC_AGENT_SKIP_REFINEMENT", "false").lower() == "true",
            max_requeue_per_function=int(os.environ.get("BMC_AGENT_MAX_REQUEUE_PER_FUNCTION", "3")),
            include_dirs=[d for d in os.environ.get("BMC_AGENT_INCLUDE_DIRS", "").split(":") if d],
            cc_path=os.environ.get("BMC_AGENT_CC_PATH", "cc"),
            preprocess=os.environ.get("BMC_AGENT_PREPROCESS", "false").lower() == "true",
            enable_dynamic_validation=os.environ.get("AMC_ENABLE_DYNAMIC_VALIDATION", os.environ.get("BMC_AGENT_ENABLE_DYNAMIC_VALIDATION", "false")).lower() == "true",
            dynamic_validation_timeout=int(os.environ.get("BMC_AGENT_DYNAMIC_VALIDATION_TIMEOUT", "30")),
            dynamic_cc_path=os.environ.get("BMC_AGENT_DYNAMIC_CC_PATH", "gcc"),
            enable_realism_check=os.environ.get("AMC_ENABLE_REALISM_CHECK", "false").lower() == "true",
            enable_realism_thinking=os.environ.get("AMC_ENABLE_REALISM_THINKING", "false").lower() == "true",
            enable_flag_selection=os.environ.get("BMC_AGENT_ENABLE_FLAG_SELECTION", "false").lower() == "true",
            threat_model=os.environ.get("AMC_THREAT_MODEL", "security").lower(),
        )
