"""
Configuration dataclass for BMC-Agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_role_overrides_env() -> "dict[str, dict[str, str]]":
    """Build the per-role override map from environment variables.

    Two opt-in shapes:

    1. **Hybrid quick-start.** Set ``BMC_AGENT_HYBRID_SPEC_GEN_KEY`` (typically
       an OpenRouter key) and bmc-agent will route spec_gen + feedback_distill
       to ``anthropic/claude-sonnet-4.5`` via OpenRouter, leaving every other
       call on the global default (K2 etc.). Optional overrides for the model
       and base URL: ``BMC_AGENT_HYBRID_SPEC_GEN_MODEL``,
       ``BMC_AGENT_HYBRID_SPEC_GEN_BASE_URL``.

    2. **Explicit per-role.** For each role X in {spec_gen, feedback_distill,
       refinement, realism, classifier, disagreement_diagnose}, the env vars
       ``BMC_AGENT_LLM_{X}_MODEL`` / ``_BASE_URL`` / ``_API_KEY`` / ``_PROVIDER``
       are picked up directly. Useful for non-hybrid custom routing.

       The actual call sites use:
         * ``spec_gen``               — Phase 1 caller-grounded spec drafting
         * ``realism``                — Phase 3 CEx classification + tool-use
                                         augmentation
         * ``refinement``             — spec_refiner + LLM-fallback reachability
         * ``feedback_distill``       — UNREALISTIC → learned-clause distillation
         * ``disagreement_diagnose``  — Phase 3d three-oracle diagnosis
         * ``classifier``             — declared but currently unused

    Empty result (no env vars set) leaves ``llm_role_overrides`` empty so the
    pipeline keeps its existing single-backend behaviour.

    The global fallback (when a role has no per-role override) reads:
        ``BMC_AGENT_LLM_DEFAULT_MODEL`` (preferred) →
        ``BMC_AGENT_LLM_MODEL`` (legacy)
    Same fallback ladder for ``API_KEY``, ``BASE_URL``, ``PROVIDER``.
    """
    overrides: dict[str, dict[str, str]] = {}

    # Hybrid quick-start: spec_gen + feedback_distill → Claude on OpenRouter.
    hybrid_key = os.environ.get("BMC_AGENT_HYBRID_SPEC_GEN_KEY", "")
    if hybrid_key:
        hybrid_model = os.environ.get(
            "BMC_AGENT_HYBRID_SPEC_GEN_MODEL", "anthropic/claude-sonnet-4.5"
        )
        hybrid_base = os.environ.get(
            "BMC_AGENT_HYBRID_SPEC_GEN_BASE_URL", "https://openrouter.ai/api/v1"
        )
        # OpenRouter exposes an OpenAI-compatible /v1/chat/completions endpoint,
        # so route through the openai provider regardless of the model name.
        for role in ("spec_gen", "feedback_distill"):
            overrides[role] = {
                "model": hybrid_model,
                "base_url": hybrid_base,
                "api_key": hybrid_key,
                "provider": "openai",
            }

    # Explicit per-role overrides via BMC_AGENT_LLM_<ROLE>_* env vars.
    #
    # Roles correspond to the ``role=...`` argument threaded through
    # LLMClient.complete() at every call site. Adding a new role here
    # just makes it env-overridable; the call site code uses whatever
    # string it always uses. ``classifier`` is retained for back-compat
    # though no current call site uses it.
    for role in (
        "spec_gen",
        "feedback_distill",
        "refinement",
        "realism",
        "classifier",
        "disagreement_diagnose",
        "triage",
        "dynamic_repro",
        "dynval_triage",
        "cbmc_driver",
    ):
        ru = role.upper()
        model = os.environ.get(f"BMC_AGENT_LLM_{ru}_MODEL", "")
        base = os.environ.get(f"BMC_AGENT_LLM_{ru}_BASE_URL", "")
        key = os.environ.get(f"BMC_AGENT_LLM_{ru}_API_KEY", "")
        provider = os.environ.get(f"BMC_AGENT_LLM_{ru}_PROVIDER", "")
        if any((model, base, key, provider)):
            # Explicit role override merges with (and overrides) the hybrid
            # quick-start for this role.
            existing = overrides.get(role, {})
            overrides[role] = {
                "model": model or existing.get("model", ""),
                "base_url": base or existing.get("base_url", ""),
                "api_key": key or existing.get("api_key", ""),
                "provider": provider or existing.get("provider", ""),
            }
    return overrides


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
    # path. Default sized to accommodate reasoning models on the openai path
    # (K2 Think regularly takes 60-180s on a complex spec-gen prompt).
    llm_request_timeout_s: float = 300.0
    # Provider dispatch:
    #   "anthropic"        -- native Anthropic Messages API (claude-* via api.anthropic.com
    #                          or OpenRouter proxy)
    #   "openai"           -- OpenAI-compatible /v1/chat/completions (K2 Think, OpenAI,
    #                          most self-hosted endpoints)
    #   "claude-code"      -- shell out to the local Claude Code CLI (`claude -p`).
    #                          No API key required: uses the host's existing login.
    # Empty string => auto-detect: claude-code when no API key is set anywhere,
    # otherwise openai for K2-Think / /v1 base URLs and anthropic for the rest.
    llm_provider: str = ""

    # Path to the Claude Code CLI binary, used only when provider == "claude-code".
    # Override via BMC_AGENT_CLAUDE_CODE_BIN if `claude` isn't on $PATH.
    claude_code_bin: str = "claude"

    # Per-call timeout for the claude-code provider. The local ``claude -p``
    # path has ~5-6k tokens of fixed CLI overhead per call and runs serially,
    # so the API-mode default (180s) is too tight for prompts that legitimately
    # produce thousands of output tokens (e.g. reproducer generation, large
    # spec-gen). Bumped to 600s by default; override via
    # ``BMC_AGENT_CLAUDE_CODE_TIMEOUT_S``. Ignored when provider != claude-code.
    claude_code_timeout_s: float = 600.0

    # Agentic claude-code mode. When True (and provider == "claude-code"), the
    # CLI is granted the read-only tools in ``claude_code_tools`` and is allowed
    # to read files under ``claude_code_add_dirs`` while it answers — so spec
    # generation / refinement can actually go read caller sites and adjacent
    # code to ground a precondition, instead of a one-shot text completion.
    # Off by default (text-only, identical shape to the API path). Toggle via
    # ``--claude-code-agentic`` or ``BMC_AGENT_CLAUDE_CODE_AGENTIC=1``.
    claude_code_agentic: bool = False
    # Read-only tool allowlist handed to ``claude -p`` in agentic mode. Keep it
    # read-only (no Bash/Write/Edit) so a spec-gen call can't mutate the tree.
    claude_code_tools: str = "Read,Grep,Glob"
    # Directories the agentic claude-code call may read. Populated by the CLI
    # from the source file's directory + any --include-dir. cwd is always
    # readable regardless; this widens access to the project tree.
    claude_code_add_dirs: "list[str]" = field(default_factory=list)
    # Permission mode for the agentic call. ``dontAsk`` auto-denies anything
    # outside the allowlist silently (no interactive prompt / hang).
    claude_code_permission_mode: str = "dontAsk"

    # Per-role LLM overrides for hybrid backends. Maps a role name (e.g.
    # "spec_gen", "feedback_distill") to a partial settings dict with
    # any subset of {"model", "base_url", "api_key", "provider"}. When
    # ``complete(..., role=X)`` is called and X is in this dict, the call
    # uses the override settings (falling back to the global defaults for
    # any unset field). Roles not in the dict use the global config.
    #
    # Canonical hybrid setup: route spec_gen + feedback_distill through
    # Claude (higher spec quality) while keeping the workhorse refinement,
    # realism, and classifier calls on K2 (token volume). Empty by default,
    # so existing single-backend behaviour is unchanged.
    llm_role_overrides: "dict[str, dict[str, str]]" = field(default_factory=dict)

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

    # When True, instantiate the legacy v1 SpecGenerator instead of the
    # default v2 SpecGeneratorV2. v2 is caller-grounded with provenance
    # tags and is the default; v1 remains accessible for parity
    # comparison and as an escape hatch during the v2 rollout cycle.
    # Set via ``--legacy-spec-gen`` on verify / verify-dir.
    use_legacy_spec_gen: bool = False

    # Realism-feedback-driven in-sweep spec refinement. When True and
    # realism rejects a CEx with verdict=UNREALISTIC plus a concrete
    # key_concern, spec_refiner emits the targeted clause that would
    # exclude the rejected CEx, re-runs BMC, and applies the soundness
    # acceptance check (targeted CEx gone AND no previously-realistic
    # CEx silently dropped). Opt-in via ``--enable-spec-refiner``;
    # defaults off so existing pipeline behaviour is unchanged.
    enable_spec_refiner: bool = True

    # Caller-grounded soundness gate on refinement (Phase 3b). Before a
    # refiner-proposed clause is AND'd into the precondition, an agentic
    # SoundnessAgent checks whether the clause is actually guaranteed by every
    # caller, or whether adopting it would MASK a reachable path. On a confident
    # UNSOUND verdict the refinement is BLOCKED (the counterexample survives as a
    # real-bug lead instead of being assumed away). UNKNOWN/SOUND let the
    # refinement proceed, so a non-agentic backend (which returns UNKNOWN here)
    # degrades to the pre-gate behaviour. Opt-in; pairs with
    # ``--specs-via-claude-code --claude-code-agentic`` (the gate shares the
    # refinement routing role). Toggle: ``--enable-soundness-gate`` /
    # ``BMC_AGENT_ENABLE_SOUNDNESS_GATE``.
    enable_soundness_gate: bool = False

    # LLM-driven inline-vs-stub advisor for callees that the mechanical
    # rule (file-local static, ≤30 LoC, no loops/alloc/recursion) marked
    # STUB. The advisor reconsiders them and may PROMOTE some to inline
    # when the body is a small predicate / getter whose stub would
    # produce stub-disconnect FPs. Opt-in via ``--enable-inlining-advisor``;
    # defaults off so existing pipeline behaviour is unchanged.
    enable_inlining_advisor: bool = True

    # spec_gen v2.2: bounded LLM tool use during spec drafting. When the
    # base v2 spec for a function flags spec_disagreement (body vs callers
    # contradict) OR has no caller evidence (vtable-only / orphan), v2.2
    # fires a second LLM call with tools (lookup_function, find_more_callers,
    # lookup_struct, lookup_caller_spec, grep_corpus) so the LLM can fetch
    # authoritative data mid-reasoning. Bounded: max 5 tool calls per spec,
    # 8 LLM turns. Default-on; --no-spec-gen-tools to disable.
    enable_spec_gen_tools: bool = True

    # Realism check with bounded tool use. When the base realism check
    # returns UNCERTAIN/UNREALISTIC, fires a second LLM call with tools
    # (walk_call_chain, lookup_function, lookup_callee_postcondition)
    # so the LLM can verify call chains against the parsed corpus
    # instead of hallucinating them. REALISTIC verdicts are kept as-is
    # (never weakened by augmentation). Bounded: max 3 tool calls, 6
    # LLM turns. Default-on; --no-realism-tools to disable.
    enable_realism_tools: bool = True

    # Raw-bytes mode: treat single ``char *`` / ``const char *`` parameters as
    # raw byte buffers instead of bounded NUL-terminated strings in the harness.
    # Required for wire-format parsers (protobuf upb varints, length-prefixed
    # blobs) that read N raw bytes from ``ptr[0..N)`` regardless of NULs.  The
    # NUL-string default over-constrains the input (no embedded NULs) and
    # under-sizes the backing buffer when the function reads beyond strlen.
    raw_bytes: bool = False

    # Struct-pointer field validity inference. When True, primitive-pointer
    # fields (``float *``, ``int *``, ``double *``, etc.) of struct
    # parameters get a disjunctive harness init: either NULL, or a fresh
    # backing buffer of ``cbmc_unwind + 1`` elements. Without this, CBMC's
    # nondet pointer model allows "non-NULL but invalid", which crashes any
    # ``memset(field, ...)`` even when the source has a proper
    # ``if (field != NULL)`` guard (the guard passes, the access on
    # non-NULL-invalid then traps). Target audience: ML / numerics codebases
    # (llm.c, ggml) whose struct pointer fields are typed ``float *`` and
    # never NUL-terminated, so the existing char-string heuristic skips them.
    # Safe default off; turn on for ML-kernel-style targets via
    # ``BMC_AGENT_INFER_FIELD_VALIDITY=true``.
    infer_field_validity: bool = False

    # M1.3 — struct-pointer field validity. Extends M1's disjunctive
    # init pattern from primitive-pointer fields to struct-pointer and
    # union-pointer fields. When True, ``struct T *field`` inside a
    # struct parameter becomes NULL or a malloc'd 256-byte buffer cast
    # to the struct pointer type.
    #
    # NOTE: Verdict-impact is target-dependent.
    # - ML / disciplined-NULL-check code (llm.c): may help verify
    #   functions that NULL-check before deref.
    # - Kernel code (AWS Neuron driver): regresses ~5-10% of verifieds
    #   because the explicit NULL branch exposes
    #   defensive-programming gaps in functions that don't NULL-check
    #   caller-provided handles.
    #
    # Empirically tested on neuron_dma.c: 33/60 baseline → 28/60
    # with this flag on. So default OFF; opt in via
    # ``BMC_AGENT_INFER_STRUCT_FIELD_VALIDITY=true`` when the target
    # has disciplined NULL-checking.
    infer_struct_field_validity: bool = False

    # Top-level array-parameter bounds inference. When True, a top-level
    # pointer parameter ``T *param`` of a known primitive type
    # (``size_t *``, ``int *``, ``float *``, …) gets a backing array
    # sized from the maximum literal subscript found in the function
    # body. The default single-element local backing leaves the harness
    # exploring writes to ``param[1]..param[15]`` against a 1-element
    # object, producing a pointer-OOB false positive on functions like
    # llm.c's ``fill_in_parameter_sizes`` that write a fixed-size
    # parameter table. With this flag on, the body-scan finds the
    # ``param_sizes[15]`` write and sizes the backing to 16.
    # If no literal subscripts are found, falls back to ``cbmc_unwind+1``.
    # Capped at ``infer_array_param_bounds_max`` (default 64) to prevent
    # runaway sizing on functions that subscript by macros that the
    # parser couldn't resolve.
    infer_array_param_bounds: bool = False
    infer_array_param_bounds_max: int = 64

    # Scale-down mode for ML / numerics kernels (M2). When True, the
    # harness adds upper-bound __CPROVER_assume clauses to value
    # parameters whose names match ML parametric-size conventions
    # (``B``, ``T``, ``C``, ``NH``, ``V``, ``Vp``, ``OC``, ``N``,
    # ``batch_size``, ``seq_len``, ``num_heads``, ``channels``,
    # ``vocab_size``, ``padded_vocab_size``, ``num_layers``). Each
    # such param is constrained to ``[0, scale_down_size]``, making
    # float-arithmetic kernels (matmul, attention, layernorm,
    # softmax) tractable at scaled-down problem sizes instead of
    # running CBMC against arbitrarily-large B*T*C inner loops.
    # The LLM-generated precondition's lower-bound clauses (e.g.
    # ``B > 0``) compose with these upper bounds to give a small
    # exploration space without contradicting the spec.
    # Default off; turn on with ``BMC_AGENT_SCALE_DOWN=true``.
    scale_down: bool = False
    scale_down_size: int = 4

    # Safety-only spec mode (M3). When True, the spec-generation prompt
    # instructs the LLM to restrict postconditions to memory safety,
    # range bounds, and NaN/Inf-freedom — forbidding functional /
    # algebraic correctness postconditions that the SMT solver can't
    # bound at scale (associativity-dependent claims, exact float
    # arithmetic equivalence, complex algebraic identities).
    # The right default for ML / numerics kernels in scale-down mode:
    # we want a clean "memory-safe + no-NaN" verdict, not a vacuous
    # functional-spec attempt that times out. Off by default; pairs
    # naturally with ``BMC_AGENT_SCALE_DOWN``.
    safety_only: bool = False

    # Kani (Rust BMC) settings — parallels CBMC.  Kani's defaults are higher
    # than CBMC's; the unwind is left at None so kani picks its own when
    # absent (we still surface the field to give the pipeline a single knob).
    kani_path: str = "kani"
    kani_unwind: int = 4
    kani_timeout: int = 120  # seconds
    # Cargo-mode for Kani: run the harness as a test inside the host crate
    # via `cargo kani --tests --harness <name>` instead of as a standalone
    # `kani harness.rs` invocation. Required for multi-crate workspace
    # targets (ast-grep, ruff_python_parser, etc.) where harness emit can't
    # resolve cross-crate imports in standalone mode. When True, the
    # harness file is placed at `<crate_root>/tests/__bmc_<driver>.rs`,
    # cargo kani is invoked from the crate root, and the file is removed
    # after verification.
    kani_real_crate: bool = False
    # Simplify Phase 1 specs to maximise Kani solver tractability.
    # When True, the spec parser drops `functional_spec` (which Claude often
    # emits as nested iter().fold(...) reference-equivalence expressions that
    # cause Kani's SMT solver to hang at trivial-looking functions). Defensive
    # panic-class checks (slice OOB, overflow add) remain. This is the right
    # default in cargo-mode because the harness has to compile + verify
    # against the whole crate; complex specs blow up the proof obligation
    # size. Verified manually on adler::adler32_slice: full spec → cargo kani
    # hangs at 60s; simplified spec → 1.3s verify of 482 properties.
    simple_specs: bool = False
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

    # CEx dedup window: how many counterexamples to keep per property type
    # (e.g. how many distinct pointer_dereference.N CExs to forward to
    # classification + realism check). 1 = original behaviour (drop deeper
    # indices); 3 = default for surfacing artifact-masked real bugs.
    dedup_max_per_type: int = 3

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
    enable_dynamic_validation: bool = True   # compile and run a GCC harness to confirm real faults
    dynamic_validation_timeout: int = 30     # seconds to allow the compiled harness to run
    dynamic_cc_path: str = "gcc"             # C compiler for dynamic harness compilation

    # Flag selector settings (Phase 1.5: per-function CBMC flag selection)
    enable_flag_selection: bool = True       # LLM selects per-function CBMC flags (e.g. --unsigned-overflow-check)

    # Agentic harness gen: replace the deterministic HarnessGenerator with an
    # LLM tool-using call (bmc_agent/agentic_harness_gen.py). The LLM reads
    # callees/callers, decides per-callee stub-vs-inline, sizes buffers to
    # match real callers, and emits a complete harness.  Fallback to
    # deterministic gen if the LLM cannot produce something CBMC parses.
    enable_agentic_harness: bool = False

    # Agentic harness-repair FALLBACK. When the deterministic harness fails to
    # BUILD (CBMC conversion / incomplete-type / parse error — not a property or
    # resource failure), rebuild it with the agentic, code-reading
    # AgenticHarnessGen (which reads the real structs/headers/callers and
    # compile-checks with retry), then re-run CBMC. Distinct from
    # ``enable_agentic_harness`` (which uses the agentic builder as the PRIMARY
    # generator): this only fires on a build error, so there is no soundness
    # downside — a non-building harness yields no verdict either way. Opt-in:
    # ``--enable-agentic-harness-repair`` / ``BMC_AGENT_ENABLE_AGENTIC_HARNESS_REPAIR``.
    enable_agentic_harness_repair: bool = False

    # Split spec generation (pass 1 / pass 2). When True, V2 keeps its
    # caller-grounded POSTCONDITION + callee stubs (pass 1, where reading real
    # code helps accuracy) but regenerates the PRECONDITION via a separate
    # contract-only pass (pass 2) using the union / keep-structural-validity /
    # drop-data-value policy — so the precondition encodes the function's
    # tolerance contract, not what the observed callers happen to pass (which
    # would assume bugs away at generation time, upstream of the soundness
    # gate). Applies to the LLM-generated spec path only; the conservative
    # boundary / handle-contract short-circuits are left untouched. Opt-in;
    # turned on by --agentic. Toggle: ``BMC_AGENT_SPEC_GEN_SPLIT``.
    enable_split_spec_gen: bool = False
    # When the judge rules a CEx UNREALISTIC and the agentic harness is on,
    # hand the verdict reasoning + harness + witness back to the agentic
    # generator so it can rewrite the harness. Bounded by this round count.
    # Default 0 (disabled). Unlike the legacy feedback loop, the LLM (not
    # a regex) decides whether to incorporate the judge's reasoning and how.
    agentic_refine_rounds: int = 0

    # Realism checker settings (Phase 3 post-validation LLM audit)
    enable_realism_check: bool = True        # LLM agent that audits REAL_BUG findings for realistic exploitability

    # Counterexample classifier (Phase 3 S1 = CExValidator): the LLM+conventional
    # step that labels each CBMC cex REAL_BUG / SPURIOUS / UNRESOLVED (and drives
    # the SPURIOUS→refinement→soundness-gate loop). Default on. When OFF, every cex
    # is surfaced as a raw UNRESOLVED lead with no LLM classification and no
    # refinement — so under --agentic (which defaults it off) the dynamic
    # reproducer becomes the gate. Independent of realism/triage. Toggle:
    # ``--enable-classifier`` (re-enable under --agentic) / ``BMC_AGENT_ENABLE_CLASSIFIER``.
    enable_classifier: bool = True
    enable_realism_thinking: bool = False    # use extended thinking in the realism checker (slower, higher quality)

    # Phase 3e — in-pipeline TriageToolsAgent oracle. After Phase 3b
    # drains the caller-recheck queue, every UNRESOLVED counterexample
    # gets an independent triage verdict from a tool-augmented agent
    # that walks the call chain and audits size calculators against
    # writers. REAL_BUG/high verdicts promote to bug reports; LIKELY_FP
    # verdicts are recorded for downstream consumers but kept in the
    # unresolved bucket. Default off — the agent is expensive (~10-iter
    # tool-use loop per CEx) and the post-hoc ``scripts/triage_unresolved.py``
    # already provides the same data outside the pipeline.
    enable_phase_3e_triage: bool = False

    # Feedback loop: distill UNREALISTIC verdicts into learned constraints
    # or code-change TODOs (see bmc_agent/feedback_loop.py). The harness
    # generator auto-applies learned function/project clauses on the next
    # sweep so the same artifact pattern stops re-appearing. Default-on
    # as part of the recommended pipeline (use --no-feedback-loop to disable).
    enable_feedback_loop: bool = True
    # In-sweep convergence: after distilling a clause and persisting it,
    # immediately re-run CBMC on the same function (with the new harness
    # picking up the clause via Step 1.7). Loop until the function
    # verifies clean, a REALISTIC verdict emerges, the new CE is the
    # same class as the previous one (clause was a no-op), or
    # ``feedback_max_iters`` is exhausted.
    feedback_max_iters: int = 3

    # Threat model — shapes CBMC baseline flags, spec prompts, and realism context.
    # "security"   (default): memory safety + integer overflow, attacker-controlled inputs.
    # "safety"     : functional correctness + no-crash, valid system state.
    # "functional" : spec correctness only, no extra CBMC checks.
    threat_model: str = "security"

    # Free-text trust-boundary note (distinct from the ``threat_model`` mode
    # enum above). Describes, for THIS target, which inputs are
    # attacker-controlled vs. caller/hardware-guaranteed — the project's trust
    # boundary in prose. Injected into every trust-deciding role (spec_gen,
    # refinement, classifier, dynamic_repro, dynval_triage, realism) so the
    # precondition is shaped correctly AT GENERATION TIME rather than patched
    # post-hoc by the realism filter. Empty = no note (roles fall back to the
    # conservative default: treat inputs as attacker-controlled). See
    # ``llm.threat_model_context``.
    threat_model_note: str = ""

    # Lite mode: skip LLM spec_gen entirely. Every function gets a permissive
    # (pre=post=true) spec, the harness inputs are nondet (subject to the
    # global harness flags), and CBMC's built-in checks (--bounds-check,
    # --pointer-check, --signed-overflow-check) surface memory-safety bugs
    # directly. The LLM budget shifts to realism + classifier in Phase 3,
    # where the LLM adds real signal rather than parroting the function body.
    # Also skips Pass 1.5 (domain knowledge extraction) since that feeds
    # spec_gen prompts. Off by default to preserve existing behaviour.
    lite_mode: bool = False

    # Universal contracts for lite-mode: when True (default), the
    # permissive spec emitted in lite-mode is enriched with
    # deterministic preconditions derived from parameter names alone
    # (no LLM). Today's contracts emit paired-pointer ordering
    # (``start <= end``, ``src <= dst``, etc.) for the canonical name
    # pairs in ``bmc_agent.universal_contracts._PAIRED_POINTER_NAMES``.
    # The existing ``_detect_paired_pointers`` in harness_generator
    # picks up these clauses and allocates a single shared backing
    # buffer per pair, eliminating the dominant caller-contract-slip
    # FP class on userland libraries (libarchive ismode/isint/etc.).
    # Disable to reproduce the pure pre=true behaviour for ablation.
    lite_with_contracts: bool = True

    # ------------------------------------------------------------------
    # Autonomous mode — session-mutable strip sets (Phase 1).
    # ------------------------------------------------------------------
    # The auto-retry layer (bmc_agent.auto_retry_registry) populates these
    # at runtime when a CBMC error has a known structural recovery (e.g.
    # "type symbol 'foo_t' defined twice" → add foo_t here so the next
    # harness regen strips the harness's variant).
    #
    # They extend the static sets in harness_generator without requiring
    # a code change. Successful entries are candidates for promotion into
    # ``_SYSTEM_TYPEDEF_NAMES`` / ``_GLIBC_KNOWN_STRUCTS`` after human
    # review (auto_retries.json log).
    #
    # All three default to empty list so behaviour is unchanged unless
    # the autonomous loop populates them.

    # Typedef names to strip in addition to ``_SYSTEM_TYPEDEF_NAMES``.
    # Driven by RetryAction.ADD_TYPEDEF_TO_STRIP.
    session_strip_typedefs: list[str] = field(default_factory=list)

    # Struct/union tag names to strip body for in addition to
    # ``_GLIBC_KNOWN_STRUCTS``. Driven by RetryAction.ADD_STRUCT_TO_STRIP.
    session_strip_structs: list[str] = field(default_factory=list)

    # Struct tag names whose params should be emitted as nondet pointers
    # (no stack-allocated backing) regardless of whether their body is
    # present in struct_definitions. Driven by RetryAction.FORCE_OPAQUE_PARAM.
    session_opaque_param_structs: list[str] = field(default_factory=list)

    # Local function names whose BODY should be replaced with a nondet
    # stub during harness emit. Driven by RetryAction.STUB_CALLEE — when
    # CBMC times out on a function whose state space is dominated by an
    # inlined callee, replacing the callee's body with a nondet return
    # cuts the state space dramatically. In ``--real-libc`` mode the
    # harness includes the whole preprocessed source, so all callee
    # bodies live in the same translation unit; this set drives the
    # source post-processing that replaces selected bodies with stubs.
    # Tradeoff vs. NO stubbing: a stubbed callee can hide a bug that
    # lives inside it (the verifier no longer explores that code). The
    # auto-retry path only stubs as a recovery from TIMEOUT — without
    # this, the timed-out function is silently dropped, which is
    # strictly worse than partial coverage.
    session_stub_functions: list[str] = field(default_factory=list)

    # Max number of CBMC-error retry rounds the autonomous-mode Phase 2b
    # loop will run before giving up. Each round can resolve many
    # functions if they share an error identifier — empirically on
    # libarchive, ~4612 of 4829 errors were "syntax error before '<id>'"
    # for a tiny set of typedefs (off64_t, fpos64_t, btowc, ...), so the
    # first round usually unblocks the bulk of the sweep. Round 2 catches
    # second-order errors that surface only after the first round's fixes
    # land. 0 disables auto-retry entirely.
    auto_retry_max_rounds: int = 2

    # Phase 4b — extra skepticism context for the realism checker.
    # Populated by ``bmc_agent.realism_hint_injector.collect_hints``
    # after each autonomous round when an FP pattern (UNINIT_VTABLE,
    # UNINIT_CONTAINER, …) recurred above the threshold. The realism
    # checker prepends this to its system prompt for the *next* round,
    # so the LLM applies stronger skepticism to recurring FP classes
    # without us editing the static prompt template. Empty string
    # means no learned hints — the realism prompt runs unchanged.
    realism_extra_skepticism: str = ""

    # Phase 3 — self-patch agent mode. One of:
    #   "deny"  (default) — the self-patch agent is OFF. CBMC errors
    #                        with no registered retry action just stay
    #                        errored. Safe-by-default; no LLM is asked
    #                        to edit bmc-agent source.
    #   "stage" — when Phase 1 returns NO_ACTION, the agent proposes
    #             a patch to harness_generator.py / preprocessor.py
    #             plus a regression test, runs all safety gates
    #             (allow-list, scope cap, fail-before / pass-after),
    #             and on success writes the diff + test source to
    #             ``<output>/proposed_patches/round_<N>/``. Working
    #             tree is left clean; operator reviews and applies.
    #   "auto"  — same as stage, plus apply via ``git apply`` and
    #             commit. Reserved for trusted-target sweeps; the
    #             operator opts in explicitly.
    # See ``bmc_agent.self_patch_agent`` for the safety-gate logic.
    allow_self_patch: str = "deny"

    def resolved_api_key(self) -> str:
        """Return the effective API key, reading from env if not set directly.

        Priority: ``llm_api_key`` field → ``BMC_AGENT_LLM_API_KEY`` →
        ``K2THINK_API_KEY`` (when provider resolves to openai) →
        ``ANTHROPIC_API_KEY``. The ``claude-code`` provider doesn't need
        a key (it shells out to the locally-logged-in CLI), so this can
        legitimately return an empty string for that provider.
        """
        if self.llm_api_key:
            return self.llm_api_key
        bmc_key = os.environ.get("BMC_AGENT_LLM_API_KEY", "")
        if bmc_key:
            return bmc_key
        if self.resolved_provider() == "openai":
            k2_key = os.environ.get("K2THINK_API_KEY", "")
            if k2_key:
                return k2_key
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def role_settings(self, role: str | None) -> dict:
        """Return the effective LLM settings for a given role.

        Returns a dict with keys ``model``, ``base_url``, ``api_key``, ``provider``,
        each falling back to the global config when the role-specific override
        doesn't set them. ``role=None`` (or a role not in ``llm_role_overrides``)
        returns the global defaults.
        """
        override = self.llm_role_overrides.get(role or "", {}) if role else {}
        return {
            "model": override.get("model") or self.llm_model,
            "base_url": override.get("base_url") or self.llm_base_url,
            "api_key": override.get("api_key") or self.resolved_api_key(),
            "provider": override.get("provider") or self.llm_provider,
        }

    def resolved_provider(self) -> str:
        """Return the active provider ("anthropic", "openai", or "claude-code").

        If ``llm_provider`` is set explicitly, honour it. Otherwise auto-detect:

        * K2 Think and other OpenAI-compatible base URLs route to "openai".
        * If no API key is set anywhere (and no explicit base_url suggests
          openai), route to "claude-code" so the local Claude Code CLI is
          used — this is the zero-config default.
        * Everything else (Anthropic key set, OpenRouter, etc.) routes to
          "anthropic".
        """
        if self.llm_provider:
            return self.llm_provider
        base = (self.llm_base_url or "").lower()
        if "k2think.ai" in base or base.endswith("/v1") or base.endswith("/v1/"):
            return "openai"
        # Zero-config default: if no API key was found in any of the usual
        # places, fall back to the local Claude Code CLI.
        if not (
            self.llm_api_key
            or os.environ.get("BMC_AGENT_LLM_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("K2THINK_API_KEY", "")
            or os.environ.get("BMC_AGENT_HYBRID_SPEC_GEN_KEY", "")
        ):
            return "claude-code"
        return "anthropic"

    @classmethod
    def from_env(cls) -> "Config":
        """Create a Config populated from environment variables where available.

        Global LLM settings: ``BMC_AGENT_LLM_DEFAULT_*`` is the preferred
        name (clearer alongside the per-role ``BMC_AGENT_LLM_<ROLE>_*``
        env vars). ``BMC_AGENT_LLM_*`` is the legacy name and still works
        as a fallback. Either form sets the global default that role
        overrides build on.
        """
        return cls(
            llm_model=(
                os.environ.get("BMC_AGENT_LLM_DEFAULT_MODEL", "")
                or os.environ.get("BMC_AGENT_LLM_MODEL", "")
                or "claude-sonnet-4-6"
            ),
            llm_api_key=(
                os.environ.get("BMC_AGENT_LLM_DEFAULT_API_KEY", "")
                or os.environ.get("BMC_AGENT_LLM_API_KEY", "")
                or os.environ.get("ANTHROPIC_API_KEY", "")
                or os.environ.get("K2THINK_API_KEY", "")
            ),
            llm_base_url=(
                os.environ.get("BMC_AGENT_LLM_DEFAULT_BASE_URL", "")
                or os.environ.get("BMC_AGENT_LLM_BASE_URL", "")
            ),
            llm_request_timeout_s=float(os.environ.get("BMC_AGENT_LLM_TIMEOUT_S", "180.0")),
            llm_provider=(
                os.environ.get("BMC_AGENT_LLM_DEFAULT_PROVIDER", "")
                or os.environ.get("BMC_AGENT_LLM_PROVIDER", "")
            ),
            claude_code_bin=os.environ.get("BMC_AGENT_CLAUDE_CODE_BIN", "claude"),
            claude_code_timeout_s=float(os.environ.get("BMC_AGENT_CLAUDE_CODE_TIMEOUT_S", "600.0")),
            claude_code_agentic=os.environ.get("BMC_AGENT_CLAUDE_CODE_AGENTIC", "false").lower()
            in ("1", "true", "yes"),
            claude_code_tools=os.environ.get("BMC_AGENT_CLAUDE_CODE_TOOLS", "Read,Grep,Glob"),
            claude_code_permission_mode=os.environ.get(
                "BMC_AGENT_CLAUDE_CODE_PERMISSION_MODE", "dontAsk"
            ),
            lite_mode=os.environ.get("BMC_AGENT_LITE_MODE", "false").lower() == "true",
            cbmc_path=os.environ.get("BMC_AGENT_CBMC_PATH", "cbmc"),
            cbmc_unwind=int(os.environ.get("BMC_AGENT_CBMC_UNWIND", "4")),
            cbmc_timeout=int(os.environ.get("BMC_AGENT_CBMC_TIMEOUT", "120")),
            cbmc_real_libc=os.environ.get("BMC_AGENT_CBMC_REAL_LIBC", "false").lower() == "true",
            inline_pure_callees=os.environ.get("BMC_AGENT_INLINE_PURE_CALLEES", "true").lower() != "false",
            inline_pure_callees_max_loc=int(os.environ.get("BMC_AGENT_INLINE_PURE_CALLEES_MAX_LOC", "30")),
            strict_dsl=os.environ.get("BMC_AGENT_STRICT_DSL", "false").lower() == "true",
            raw_bytes=os.environ.get("BMC_AGENT_RAW_BYTES", "false").lower() == "true",
            infer_field_validity=os.environ.get("BMC_AGENT_INFER_FIELD_VALIDITY", "false").lower() == "true",
            infer_struct_field_validity=os.environ.get("BMC_AGENT_INFER_STRUCT_FIELD_VALIDITY", "false").lower() == "true",
            infer_array_param_bounds=os.environ.get("BMC_AGENT_INFER_ARRAY_PARAM_BOUNDS", "false").lower() == "true",
            infer_array_param_bounds_max=int(os.environ.get("BMC_AGENT_INFER_ARRAY_PARAM_BOUNDS_MAX", "64")),
            scale_down=os.environ.get("BMC_AGENT_SCALE_DOWN", "false").lower() == "true",
            scale_down_size=int(os.environ.get("BMC_AGENT_SCALE_DOWN_SIZE", "4")),
            safety_only=os.environ.get("BMC_AGENT_SAFETY_ONLY", "false").lower() == "true",
            kani_path=os.environ.get("BMC_AGENT_KANI_PATH", "kani"),
            kani_unwind=int(os.environ.get("BMC_AGENT_KANI_UNWIND", "4")),
            kani_timeout=int(os.environ.get("BMC_AGENT_KANI_TIMEOUT", "120")),
            kani_slice_bound=int(os.environ.get("BMC_AGENT_KANI_SLICE_BOUND", "4")),
            kani_real_crate=os.environ.get("BMC_AGENT_KANI_REAL_CRATE", "false").lower() == "true",
            simple_specs=os.environ.get("BMC_AGENT_SIMPLE_SPECS", "false").lower() == "true",
            artifact_dir=os.environ.get("BMC_AGENT_ARTIFACT_DIR", "artifacts"),
            max_spec_retries=int(os.environ.get("BMC_AGENT_MAX_SPEC_RETRIES", "3")),
            max_refinement_iters=int(os.environ.get("BMC_AGENT_MAX_REFINEMENT_ITERS", "5")),
            dedup_max_per_type=int(os.environ.get("BMC_AGENT_DEDUP_MAX_PER_TYPE", "3")),
            batch_size=int(os.environ.get("BMC_AGENT_BATCH_SIZE", "10")),
            enable_dual_spec=os.environ.get("BMC_AGENT_ENABLE_DUAL_SPEC", "true").lower() != "false",
            enable_spec_quality=os.environ.get("BMC_AGENT_ENABLE_SPEC_QUALITY", "false").lower() == "true",
            skip_refinement=os.environ.get("BMC_AGENT_SKIP_REFINEMENT", "false").lower() == "true",
            max_requeue_per_function=int(os.environ.get("BMC_AGENT_MAX_REQUEUE_PER_FUNCTION", "3")),
            include_dirs=[d for d in os.environ.get("BMC_AGENT_INCLUDE_DIRS", "").split(":") if d],
            cbmc_defines=[d for d in os.environ.get("BMC_AGENT_CBMC_DEFINES", "").split(":") if d],
            cc_path=os.environ.get("BMC_AGENT_CC_PATH", "cc"),
            preprocess=os.environ.get("BMC_AGENT_PREPROCESS", "false").lower() == "true",
            enable_dynamic_validation=(os.environ.get("BMC_AGENT_ENABLE_DYNAMIC_VALIDATION") or os.environ.get("AMC_ENABLE_DYNAMIC_VALIDATION") or "true").lower() == "true",
            dynamic_validation_timeout=int(os.environ.get("BMC_AGENT_DYNAMIC_VALIDATION_TIMEOUT", "30")),
            dynamic_cc_path=os.environ.get("BMC_AGENT_DYNAMIC_CC_PATH", "gcc"),
            enable_realism_check=(os.environ.get("BMC_AGENT_ENABLE_REALISM_CHECK") or os.environ.get("AMC_ENABLE_REALISM_CHECK") or "true").lower() == "true",
            enable_classifier=(os.environ.get("BMC_AGENT_ENABLE_CLASSIFIER") or "true").lower() == "true",
            enable_realism_thinking=(os.environ.get("BMC_AGENT_ENABLE_REALISM_THINKING") or os.environ.get("AMC_ENABLE_REALISM_THINKING") or "false").lower() == "true",
            enable_phase_3e_triage=(os.environ.get("BMC_AGENT_ENABLE_PHASE_3E_TRIAGE") or "false").lower() == "true",
            enable_flag_selection=os.environ.get("BMC_AGENT_ENABLE_FLAG_SELECTION", "true").lower() == "true",
            enable_soundness_gate=os.environ.get("BMC_AGENT_ENABLE_SOUNDNESS_GATE", "false").lower()
            in ("1", "true", "yes"),
            enable_agentic_harness=os.environ.get("BMC_AGENT_ENABLE_AGENTIC_HARNESS", "false").lower() == "true",
            enable_agentic_harness_repair=os.environ.get("BMC_AGENT_ENABLE_AGENTIC_HARNESS_REPAIR", "false").lower()
            in ("1", "true", "yes"),
            enable_split_spec_gen=os.environ.get("BMC_AGENT_SPEC_GEN_SPLIT", "false").lower()
            in ("1", "true", "yes"),
            agentic_refine_rounds=int(os.environ.get("BMC_AGENT_AGENTIC_REFINE_ROUNDS", "0") or "0"),
            threat_model=(os.environ.get("BMC_AGENT_THREAT_MODEL") or os.environ.get("AMC_THREAT_MODEL") or "security").lower(),
            threat_model_note=(os.environ.get("BMC_AGENT_THREAT_MODEL_NOTE") or "").strip(),
            llm_role_overrides=_parse_role_overrides_env(),
        )
