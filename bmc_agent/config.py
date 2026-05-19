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
    # Per-request timeout for the LLM client. Without an explicit timeout the
    # Anthropic SDK can hang indefinitely on a stuck request, stalling a
    # multi-hour sweep (observed in a libxml2 run that froze for >35 minutes
    # mid-pipeline). Also used as the httpx timeout on the openai-compatible
    # path.
    llm_request_timeout_s: float = 180.0
    # Provider dispatch:
    #   "anthropic"        -- native Anthropic Messages API (claude-* via api.anthropic.com
    #                          or OpenRouter proxy)
    #   "openai"           -- OpenAI-compatible /v1/chat/completions (K2 Think, OpenAI,
    #                          most self-hosted endpoints)
    # Empty string => auto-detect from base_url (K2 Think domain, /v1 suffix, etc.).
    llm_provider: str = ""

    # CBMC settings
    cbmc_path: str = "cbmc"
    cbmc_unwind: int = 4
    cbmc_timeout: int = 120  # seconds
    # CBMC --object-bits. None = let CBMC pick its default (currently 8); with
    # cbmc_auto_scale_object_bits=True, run_cbmc will retry at 12 and 16 when
    # the "too many addressed objects" error trips. State-heavy parser files
    # (libxml2 HTMLparser.c, OpenSSL ASN.1 parsers) routinely blow past 256
    # objects; auto-scaling avoids losing those files to CBMC frontend errors.
    cbmc_object_bits: int | None = None
    cbmc_auto_scale_object_bits: bool = True
    # Inline small pure file-local callees instead of replacing them with
    # LLM-generated stubs. Reduces the "stub disconnect" false-positive
    # class — getters/predicates (jv_get_kind, xmlIsBlank_ch, BUF_ERROR)
    # where the real body trivially constrains the return but the LLM
    # contract gets it wrong. Static eligibility rules in harness_generator
    # (file-local static, ≤30 LoC, no loops, no alloc, no recursion).
    # Affects the non-real-libc path only; real-libc mode already
    # inlines everything via #include.
    inline_pure_callees: bool = True
    inline_pure_callees_max_loc: int = 30
    # Real-libc mode: emit minimal harnesses that `#include` the original
    # .c file and let CBMC do all preprocessing via -I, instead of the
    # default Python-side `cc -E` expand-then-strip pipeline. Required
    # for verifying real-world glibc-using OSS (jq, curl, OpenSSL, …);
    # leave False for bare-metal targets like VibeOS that need the
    # type stripping. Implies preprocess=False (Python doesn't expand).
    cbmc_real_libc: bool = False

    # Strict DSL mode: force Phase 1 prompts to emit pre/post as a
    # single C boolean expression (no natural language).  Required for
    # bounty / CVE workflows where prose-mixed specs translate to
    # comments and produce vacuous verifications that mask real bugs.
    # Off by default to preserve VibeOS-era behaviour.
    strict_dsl: bool = False

    # Raw-bytes mode: treat single ``char *`` / ``const char *`` parameters as
    # raw byte buffers instead of bounded NUL-terminated strings in the harness.
    # Required for wire-format parsers (protobuf upb varints, length-prefixed
    # blobs) that read N raw bytes from ``ptr[0..N)`` regardless of NULs.  The
    # NUL-string default over-constrains the input (no embedded NULs) and
    # under-sizes the backing buffer when the function reads beyond strlen.
    raw_bytes: bool = False

    # Kani (Rust BMC) settings — parallels CBMC.  Kani's defaults are higher
    # than CBMC's; the unwind is left at None so kani picks its own when
    # absent (we still surface the field to give the pipeline a single knob).
    kani_path: str = "kani"
    kani_unwind: int = 4
    kani_timeout: int = 120  # seconds
    # Bound on nondeterministic slice/array length in Kani harnesses. BMC
    # is bounded by construction; this controls how far the verifier
    # explores slice contents and indices. Default 4 keeps runtime small
    # for typical CCC-style helpers.
    kani_slice_bound: int = 4
    # Artifact settings
    artifact_dir: str = "artifacts"

    # Refinement loop settings
    max_spec_retries: int = 3
    max_refinement_iters: int = 5

    # Batch processing
    batch_size: int = 10

    # Multi-file / whole-codebase support
    include_dirs: list = field(default_factory=list)  # -I paths for cc -E
    cbmc_defines: list = field(default_factory=list)  # -D name[=value] preprocessor defines
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

    # Feedback loop: distill UNREALISTIC verdicts into learned constraints
    # or code-change TODOs (see bmc_agent/feedback_loop.py). The harness
    # generator auto-applies learned function/project clauses on the next
    # sweep so the same artifact pattern stops re-appearing.
    enable_feedback_loop: bool = False
    # In-sweep convergence: after distilling a clause and persisting it,
    # immediately re-run CBMC on the same function (with the new harness
    # picking up the clause via Step 1.7). Loop until the function
    # verifies clean, a REALISTIC verdict emerges, the new CE is the
    # same class as the previous one (clause was a no-op), or
    # ``feedback_max_iters`` is exhausted. Off by default so it doesn't
    # change non-opt-in pipeline timing.
    feedback_max_iters: int = 3

    # Threat model — shapes CBMC baseline flags, spec prompts, and realism context.
    # "security"   (default): memory safety + integer overflow, attacker-controlled inputs.
    # "safety"     : functional correctness + no-crash, valid system state.
    # "functional" : spec correctness only, no extra CBMC checks.
    threat_model: str = "security"

    def resolved_api_key(self) -> str:
        """Return the effective API key, reading from env if not set directly.

        Priority: ``llm_api_key`` field → ``K2THINK_API_KEY`` (when provider
        resolves to openai) → ``ANTHROPIC_API_KEY``.
        """
        if self.llm_api_key:
            return self.llm_api_key
        if self.resolved_provider() == "openai":
            k2_key = os.environ.get("K2THINK_API_KEY", "")
            if k2_key:
                return k2_key
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def resolved_provider(self) -> str:
        """Return the active provider ("anthropic" or "openai").

        If ``llm_provider`` is set explicitly, honour it. Otherwise auto-detect:
        K2 Think and other OpenAI-compatible base URLs route to "openai";
        everything else (default, Anthropic, OpenRouter) routes to "anthropic".
        """
        if self.llm_provider:
            return self.llm_provider
        base = (self.llm_base_url or "").lower()
        if "k2think.ai" in base or base.endswith("/v1") or base.endswith("/v1/"):
            return "openai"
        return "anthropic"

    @classmethod
    def from_env(cls) -> "Config":
        """Create a Config populated from environment variables where available."""
        return cls(
            llm_model=os.environ.get("BMC_AGENT_LLM_MODEL", "claude-sonnet-4-6"),
            llm_api_key=os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("K2THINK_API_KEY", ""),
            llm_base_url=os.environ.get("BMC_AGENT_LLM_BASE_URL", ""),
            llm_request_timeout_s=float(os.environ.get("BMC_AGENT_LLM_TIMEOUT_S", "180.0")),
            llm_provider=os.environ.get("BMC_AGENT_LLM_PROVIDER", ""),
            cbmc_path=os.environ.get("BMC_AGENT_CBMC_PATH", "cbmc"),
            cbmc_unwind=int(os.environ.get("BMC_AGENT_CBMC_UNWIND", "4")),
            cbmc_timeout=int(os.environ.get("BMC_AGENT_CBMC_TIMEOUT", "120")),
            cbmc_real_libc=os.environ.get("BMC_AGENT_CBMC_REAL_LIBC", "false").lower() == "true",
            inline_pure_callees=os.environ.get("BMC_AGENT_INLINE_PURE_CALLEES", "true").lower() != "false",
            inline_pure_callees_max_loc=int(os.environ.get("BMC_AGENT_INLINE_PURE_CALLEES_MAX_LOC", "30")),
            strict_dsl=os.environ.get("BMC_AGENT_STRICT_DSL", "false").lower() == "true",
            raw_bytes=os.environ.get("BMC_AGENT_RAW_BYTES", "false").lower() == "true",
            kani_path=os.environ.get("BMC_AGENT_KANI_PATH", "kani"),
            kani_unwind=int(os.environ.get("BMC_AGENT_KANI_UNWIND", "4")),
            kani_timeout=int(os.environ.get("BMC_AGENT_KANI_TIMEOUT", "120")),
            kani_slice_bound=int(os.environ.get("BMC_AGENT_KANI_SLICE_BOUND", "4")),
            artifact_dir=os.environ.get("BMC_AGENT_ARTIFACT_DIR", "artifacts"),
            max_spec_retries=int(os.environ.get("BMC_AGENT_MAX_SPEC_RETRIES", "3")),
            max_refinement_iters=int(os.environ.get("BMC_AGENT_MAX_REFINEMENT_ITERS", "5")),
            batch_size=int(os.environ.get("BMC_AGENT_BATCH_SIZE", "10")),
            enable_dual_spec=os.environ.get("BMC_AGENT_ENABLE_DUAL_SPEC", "true").lower() != "false",
            enable_spec_quality=os.environ.get("BMC_AGENT_ENABLE_SPEC_QUALITY", "false").lower() == "true",
            skip_refinement=os.environ.get("BMC_AGENT_SKIP_REFINEMENT", "false").lower() == "true",
            max_requeue_per_function=int(os.environ.get("BMC_AGENT_MAX_REQUEUE_PER_FUNCTION", "3")),
            include_dirs=[d for d in os.environ.get("BMC_AGENT_INCLUDE_DIRS", "").split(":") if d],
            cbmc_defines=[d for d in os.environ.get("BMC_AGENT_CBMC_DEFINES", "").split(":") if d],
            cc_path=os.environ.get("BMC_AGENT_CC_PATH", "cc"),
            preprocess=os.environ.get("BMC_AGENT_PREPROCESS", "false").lower() == "true",
            enable_dynamic_validation=(os.environ.get("BMC_AGENT_ENABLE_DYNAMIC_VALIDATION") or os.environ.get("AMC_ENABLE_DYNAMIC_VALIDATION") or "false").lower() == "true",
            dynamic_validation_timeout=int(os.environ.get("BMC_AGENT_DYNAMIC_VALIDATION_TIMEOUT", "30")),
            dynamic_cc_path=os.environ.get("BMC_AGENT_DYNAMIC_CC_PATH", "gcc"),
            enable_realism_check=(os.environ.get("BMC_AGENT_ENABLE_REALISM_CHECK") or os.environ.get("AMC_ENABLE_REALISM_CHECK") or "false").lower() == "true",
            enable_realism_thinking=(os.environ.get("BMC_AGENT_ENABLE_REALISM_THINKING") or os.environ.get("AMC_ENABLE_REALISM_THINKING") or "false").lower() == "true",
            enable_flag_selection=os.environ.get("BMC_AGENT_ENABLE_FLAG_SELECTION", "false").lower() == "true",
            threat_model=(os.environ.get("BMC_AGENT_THREAT_MODEL") or os.environ.get("AMC_THREAT_MODEL") or "security").lower(),
        )
