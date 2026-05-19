"""
Prompt templates for BMC-Agent Phase 1 spec generation and Phase 3 validation.
"""

from __future__ import annotations

# Threat model context injected into spec and realism prompts.
THREAT_MODEL_CONTEXT: dict[str, str] = {
    "security": (
        "Threat model: SECURITY. "
        "Assume inputs may be attacker-controlled. "
        "Preconditions must guard against: null/invalid pointers, integer overflow in "
        "size/offset arithmetic, out-of-bounds buffer accesses, and type-confusion from "
        "unsafe casts. Postconditions must guarantee memory safety and the absence of "
        "exploitable undefined behaviour. Flag any unchecked arithmetic on lengths, "
        "counts, or offsets derived from external data."
    ),
    "safety": (
        "Threat model: SAFETY. "
        "Focus on functional correctness and no-crash properties under valid system state. "
        "Preconditions should capture the invariants that callers are expected to maintain. "
        "Postconditions should guarantee the function terminates without crashing and leaves "
        "shared state consistent. Flag division-by-zero, null dereferences, and violated "
        "data-structure invariants."
    ),
    "functional": (
        "Threat model: FUNCTIONAL. "
        "Focus on verifying that the function satisfies its specified interface contract. "
        "Preconditions should capture the minimal valid input domain. "
        "Postconditions should capture the exact return-value and side-effect guarantees. "
        "No additional security or safety emphasis beyond the spec."
    ),
}

DSL_GRAMMAR = """\
The specification DSL:
- precondition: "requires <formula>"
- postcondition: "ensures <formula>"
- formulas can use: &&, ||, !, forall, exists
- predicates: valid(ptr), valid_string(ptr), valid_range(ptr, lo, hi), in_bounds(arr, idx), locked(lock), owns(ptr), null(ptr)
- valid_string(ptr): ptr is a non-null, null-terminated C string (use for char* parameters)
- valid_range(ptr, lo, hi): ptr is non-null and the range ptr[lo..hi) is in bounds (lo >= 0 and hi >= lo)
- arithmetic: +, -, *, /, %, <, <=, >, >=, ==, !=
- you may also write natural language conditions if formal DSL is insufficient
- return value is referred to as result
- parameters use their actual names from the function signature
"""

# Strict-formal C DSL — pre/post must be single C boolean expressions
# (only the listed predicates, only && / || / ! / arithmetic /
# comparisons). Used for bounty / CVE workflows where natural-language
# clauses translate to /* … */ comments and produce vacuous
# verifications, masking real bugs. Opt-in via Config.strict_dsl /
# `--strict-dsl`; default off to preserve the looser VibeOS-era
# behaviour where some prose mixing is tolerated.
STRICT_DSL_GRAMMAR = """\
The specification DSL — STRICTLY FORMAL FOR C:

A precondition and a postcondition must each be a SINGLE C boolean
expression. The expression is dropped verbatim (after a small set of
DSL rewrites — see below) into __CPROVER_assume(...) and assert(...),
so anything that doesn't compile as a C bool expression breaks the
harness AND silently translates to a comment, producing a vacuous
verification. Do NOT include:
  * prose, English commentary, em-dashes
  * "Let X = Y" / "If A then B" / "otherwise X" style preludes
  * inline /* … */ comments
  * sentence punctuation ('.' at end of clause, ',' separating clauses)
Combine clauses with && and ||. Quote struct fields directly
(p->curr_buf, p->stackpos, etc.).

Allowed predicates (rewritten to C by the harness generator):
  * valid(ptr)        → ptr != NULL
  * valid_string(p)   → p != NULL  (null-terminated; bound set in harness)
  * valid_range(p,l,h)→ p != NULL && l>=0 && h>=l
  * in_bounds(a, i)   → i>=0 && i<sizeof(a)/sizeof(a[0])
  * null(ptr)         → ptr == NULL
  * result           → C variable holding the return value

C-specific reminders:
  * Pointers can be NULL — write valid(p) when callers may pass NULL
    and the function dereferences it.
  * Integer arithmetic in C wraps silently on overflow; for
    unsigned*signed mixed ops, the precondition should bound the
    operand ranges so the bug class doesn't get missed.
  * Use the parameter / struct-field names from the actual signature
    — don't invent fields.

EXAMPLES of correct vs. incorrect:
  GOOD pre:  valid(p) && valid_range(buf, 0, length) && length >= 0
  GOOD post: result == 0 || result == -1
  BAD  pre:  p has been initialized by parser_init(); p->curr_buf is NULL …
  BAD  post: result == 0 if successful; otherwise result is an error pointer

If a property is genuinely too rich for a single C boolean expression
(temporal state-machine invariants, owned-vs-borrowed, etc.), emit the
WEAKEST formal expression that captures the SAFETY portion (e.g.
result != NULL, stackpos in range) and put the rest in the JSON
"reasoning" field — never mix prose into precondition / postcondition.
"""

# System prompt variants. The strict prompt swaps the DSL grammar but
# keeps the rest of the C-side preamble (threat model, struct context,
# etc.). Chosen at spec-generation time based on Config.strict_dsl.
STRICT_SPEC_SYSTEM_PROMPT = (
    f"You are a formal verification expert for C programs.\n\n{STRICT_DSL_GRAMMAR}"
)

# Rust-aware DSL notes. The core predicate vocabulary matches the C DSL —
# Phase 2 translates either form into Kani/CBMC primitives — but Rust's
# type system changes which predicates are *needed* vs *implicit*. Safe
# references are guaranteed non-null and aligned by the language, so
# valid()/null() are reserved for raw pointers inside unsafe regions.
# Slices carry length intrinsically, so valid_range becomes a bound on
# the index expression against slice.len().
RUST_DSL_GRAMMAR = """\
The specification DSL — STRICTLY FORMAL FOR RUST:

A precondition and a postcondition must each be a SINGLE Rust-boolean
expression. The expression is dropped verbatim (after a small set of DSL
rewrites — see below) into `kani::assume(...)` and `kani::assert(...)`,
so anything that doesn't compile as a Rust bool expression will break
the harness. Do NOT include:
  * prose, English commentary, or em-dashes (— or –)
  * "Let X = Y" / "If A then B" / "In all cases ..." style preludes
  * inline comments (// or /* */)
  * sentence punctuation ('.' at end, ',' separating clauses)
Combine clauses with `&&` and `||`. Quote tuple/struct fields directly
(`result.0`, `result.1`, `s.len()`).

Allowed predicates (rewritten to Rust by the harness generator):
  * in_bounds(slice, idx)   → (idx) < slice.len()
  * (A ==> B)               → (!(A) || (B))   — must be paren-wrapped
  * valid(ptr) / null(ptr)  → ptr.is_null() / !ptr.is_null()   (raw ptrs only)
  * result                 → result (Rust binding)

Rust-specific reminders (informational, not new syntax):
  * Safe references (&T, &mut T) are non-null by language guarantee —
    do not write valid(r) for them; restrict valid()/null() to raw
    pointers (*const T, *mut T) inside unsafe regions.
  * Slices (&[T], &mut [T]) carry length intrinsically — index with
    slice[i] and bound with i < slice.len().
  * Option<T> uses .is_some() / .is_none() / .unwrap() in expressions.
  * Rust integers panic on overflow in debug; for wrapping_*/checked_*
    sources the postcondition should reflect wrapped/None semantics.

EXAMPLES of correct vs. incorrect for a (u8, usize) return:
  GOOD pre:  in_bounds(input, pos)
  GOOD post: (result.1 == 1 || result.1 == 3) && (result.1 == 3 || result.0 == input[pos])
  BAD  pre:  in_bounds(input, pos) — i.e., pos < input.len(), so that ...
  BAD  post: Let (byte, advance) = result. advance == 1 || advance == 3. If advance == 3 then ...

If the spec is genuinely too rich for a single boolean expression
(trait-bound preconditions, lifetime invariants, complex state machines),
emit the WEAKEST formal expression you can defend and add the rest as a
JSON field "reasoning" — never inline the prose into precondition or
postcondition.
"""

# System prompts shared by all spec-generation calls in a session.
# Placed at module scope so the spec generator can rely on Anthropic
# prompt caching (cache_control:ephemeral, 5-minute TTL).
SPEC_SYSTEM_PROMPT = (
    f"You are a formal verification expert for C programs.\n\n{DSL_GRAMMAR}"
)
RUST_SPEC_SYSTEM_PROMPT = (
    f"You are a formal verification expert for Rust programs.\n\n{RUST_DSL_GRAMMAR}"
)


def spec_system_prompt_for(language: str, strict: bool = False) -> str:
    """Return the system prompt appropriate for *language*.

    Recognised values: ``"c"`` and ``"rust"``. Unknown languages fall
    back to the C prompt to preserve existing behaviour for callers
    that have not been updated.

    When *strict* is True and *language* is ``"c"``, the strict-formal
    C variant is returned. Strict mode forbids natural-language
    clauses in pre/post and is the right choice for bounty / CVE
    workflows where prose-mixed specs translate to vacuous
    verifications.  The Rust prompt is already strict-formal by
    default (M3c) so the flag has no effect for Rust.
    """
    if language == "rust":
        return RUST_SPEC_SYSTEM_PROMPT
    if strict:
        return STRICT_SPEC_SYSTEM_PROMPT
    return SPEC_SYSTEM_PROMPT

ENTRY_SPEC_PROMPT = """\
Your task: Given a C function's implementation and domain knowledge, generate a precise
formal specification (precondition and postcondition).

The specification should:
- Capture what the function REQUIRES of its inputs (precondition)
- Capture what the function GUARANTEES about its outputs and side effects (postcondition)
- Be tight enough to be useful for verification, not just "true"

BUG-CLASS DETECTION (postcondition strengthening):
A weak postcondition like ``result != NULL || true`` lets CBMC verify the function
as "correct" while real bugs slip through. Strengthen the postcondition to also rule
out the SPECIFIC bug classes a hostile caller could trigger:

  * **Integer overflow in size arithmetic**: when the function computes
    ``n * sizeof(T)`` or ``a + b`` for allocation/indexing, add a clause like
    ``(n <= INT_MAX / sizeof(T)) || (result == NULL)`` — i.e. either no overflow
    or the function returned the error sentinel.
  * **OOB read/write at cursor advance**: when the function advances a pointer
    or index, add a clause asserting the post-advance cursor is in-bounds.
  * **Use-after-free in cleanup**: if the function frees a resource, the
    postcondition should imply the resource pointer is NULL on return so callers
    can't reuse it.
  * **NULL deref on optional field**: when accessing ``struct->field``, the
    postcondition should be guarded by ``struct != NULL && field != NULL``
    where dereferencing happened.

Don't fabricate clauses that aren't supported by the function body — over-strong
postconditions cause VERIFIED-CLEAN runs to flip to spurious failures. Encode only
the bug-class invariants the code is actually trying to enforce; if no such
guard exists in the body, the function may genuinely have the bug.

FUNCTIONAL CORRECTNESS (``functional_spec`` field — STRONGLY ENCOURAGED):
The defensive postcondition above rules out *bug classes*; the functional spec
captures *what the function actually computes*. This is where most of the
verifier's power is. For ANY function that performs concrete computation
(arithmetic, parsing, encoding, hashing, alignment, byte ops), you SHOULD
emit a functional spec — not skip it. Leave empty only when the function
returns a structured value (AST, hashmap) that genuinely can't be expressed
as a boolean over scalars.

Concrete templates that should work for most non-trivial functions:

  * **Reference equivalence — byte readers**:
      result == u16::from_le_bytes([data[off], data[off+1]])
      result == u32::from_le_bytes([data[off], data[off+1], data[off+2], data[off+3]])
    Yes, this looks tautological. Emit it anyway — the verifier checks
    the actual bit operations match this reference, which catches
    off-by-one bit-shift bugs, endianness reversal, etc.

  * **Algebraic identities — alignment**:
      (result >= val) && (result - val < align) && (result % align == 0)
    Captures the meaning of "round val up to a multiple of align". Works
    for any align_up / align_down / round_to_boundary helper.

  * **Reference equivalence — hashing**:
      For a simple djb2-style hash (h = 5381; for b: h = h*33 + b):
        // express the fold as a sum the verifier can check up to slice_bound
        result == name.iter().fold(5381u32, |h, &b|
            h.wrapping_mul(33).wrapping_add(b as u32))
    The fold expression is exactly what the verifier needs to confirm.

  * **Round-trip identities — wrappers**:
      bytes_to_str(b, 0, b.len()) == std::str::from_utf8(b).unwrap()
        (when pre guarantees valid UTF-8)
      decode(encode(x)) == x

  * **Bit-level computation**:
      For ``high_bits(x) = x & 0xFF00``:
        result == x & 0xFF00
      For ``swap_bytes(x: u16)``:
        result == ((x & 0xFF) << 8) | ((x >> 8) & 0xFF)
    Direct equivalence to the bit formula.

WHEN TO LEAVE EMPTY:
- Function returns an AST, hashmap, or other structured value where
  equality can't be expressed as a boolean over scalars
- Function has side effects on out-of-scope state (file I/O, globals)
  that the verifier can't model
- Genuinely can't construct a reference computation simpler than the
  implementation itself (parser passes, codegen, register allocation)

ANTI-FABRICATION RULE:
A wrong functional spec triggers false-positive bug reports. The spec
must be what a correct implementation would compute, derivable from the
function name + signature + body. If you have to guess, emit empty.
Tautological specs (reference computation literally mirrors the body)
are CORRECT and should be emitted — they prove the function executes
the body without interference. Don't skip them as "obvious".

CRITICAL — SPEC-EVALUATION OVERFLOW:
The verifier runs the spec on Kani's nondeterministic inputs, which
include extremes like ``usize::MAX``, ``i64::MIN``, empty slices.
If your spec arithmetic overflows during evaluation, Kani reports
a SPURIOUS bug (the overflow is in the spec, not the body).

ALWAYS use overflow-safe operators in functional specs:
  - ``a.wrapping_add(b)`` instead of ``a + b``
  - ``a.wrapping_mul(b)`` instead of ``a * b``
  - ``a.checked_sub(b).unwrap_or(0)`` if the spec needs subtraction
  - ``a.saturating_add(b)`` when the spec is "min(a + b, MAX)"

WRONG: ``result == val + align - 1 & !(align - 1)`` — overflows when
       val near usize::MAX.
RIGHT: ``result == val.wrapping_add(align).wrapping_sub(1) & !(align - 1)``
       OR guard the spec with the precondition: ``val <= usize::MAX - align``.

If the function's contract requires no overflow (e.g. align_up assumes
the result fits in usize), express that as a PRECONDITION clause, not
as plain arithmetic in the postcondition. The verifier will then only
explore in-spec inputs and the post evaluates safely.

{threat_model_context}

Domain knowledge:
{domain_knowledge}

Struct context (constructors/definitions for struct parameters — check for assert() or
bounds constraints; these reflect invariants that ALWAYS hold on the struct fields and
MUST be included in the precondition so the verifier does not explore impossible inputs):
{struct_context}

Function signature:
{signature}

Function body:
{body}

Respond with ONLY valid JSON in this exact format:
{{
  "precondition": "<precondition in DSL or natural language>",
  "postcondition": "<postcondition in DSL or natural language — defensive bug-class clauses>",
  "functional_spec": "<optional Rust/C boolean expression specifying what a correct implementation would compute — empty string if not expressible>",
  "reasoning": "<brief explanation of why these conditions are correct, with one sentence on which bug-class invariants you encoded, and one sentence justifying the functional spec or why none was given>"
}}
"""

INTERNAL_SPEC_PROMPT = """\
Your task: Generate a specification for an INTERNAL function, taking into account
what its callers expect from it (caller-driven paradigm).

The spec should reflect CALLER INTENT - what do callers rely on this function to do?
This is stronger than just documenting the implementation: if callers assume certain
properties, the spec must guarantee them.

Caller expected specifications (what callers need from this function):
{expected_specs}

IMPORTANT: The callee function's actual signature is below. Use the parameter
names from THIS signature in your spec (e.g. if the parameter is `rb`, write
`valid(rb)` not `valid(dev->rb)` or `valid(dev)`).

{threat_model_context}

Function signature:
{signature}

Function body:
{body}

Domain knowledge:
{domain_knowledge}

Struct context (constructors/definitions for struct parameters — check for assert() or
bounds constraints; these reflect invariants that ALWAYS hold on the struct fields and
MUST be included in the precondition so the verifier does not explore impossible inputs):
{struct_context}

Merging rule for caller specs:
- precondition = disjunction (OR) of callers' expected preconditions
  (function must work if ANY caller's precondition holds)
- postcondition = conjunction (AND) of callers' expected postconditions
  (function must satisfy ALL callers' requirements)

FUNCTIONAL CORRECTNESS (optional ``functional_spec`` field):
As with entry-spec generation, you may optionally provide a behavioural
spec — a Rust/C boolean expression specifying *what the function should
compute*, not just what makes it safe. Use it to capture reference
equivalence, algebraic identities, round-trip properties, or structural
invariants. If the function's behaviour is too complex to express as a
single boolean expression (e.g. it returns a structured AST), leave
``functional_spec`` empty or ``"true"``. A wrong functional spec
produces false positives, so only emit when confident.

Respond with ONLY valid JSON in this exact format:
{{
  "precondition": "<precondition in DSL or natural language — use the callee's parameter names>",
  "postcondition": "<postcondition in DSL or natural language — use the callee's parameter names>",
  "functional_spec": "<optional behavioural spec as Rust/C boolean expression; empty string if not expressible>",
  "reasoning": "<brief explanation of why these conditions are correct, plus one sentence justifying the functional spec or why none was given>"
}}
"""

EXPECTED_SPEC_PROMPT = """\
Your task: From the perspective of a CALLER function, determine what it EXPECTS
from a callee function it calls. This is called the "expected specification."

Focus on:
1. PRECONDITION: What state does the caller establish BEFORE calling the callee?
   (What invariants hold at the call site? What are the argument values/constraints?)
2. POSTCONDITION: What does the caller RELY ON from the callee after the call returns?
   (How does the caller use the return value? What side effects does it assume?)

Caller function name: {caller_name}
Caller function signature: {caller_signature}
Caller function body:
{caller_body}

Callee function name: {callee_name}

Analyze the call site(s) where {caller_name} calls {callee_name}, and generate
the expected specification for {callee_name} from {caller_name}'s perspective.

Respond with ONLY valid JSON in this exact format:
{{
  "precondition": "<what the caller establishes before calling {callee_name}>",
  "postcondition": "<what the caller relies on {callee_name} ensuring>",
  "reasoning": "<brief explanation of your analysis>"
}}
"""

# ---------------------------------------------------------------------------
# Phase 3 prompts
# ---------------------------------------------------------------------------

REFINEMENT_PROMPT = """\
You are a formal verification expert refining function preconditions.

A BMC (Bounded Model Checker) found a counterexample for function '{function_name}',
but analysis shows it is SPURIOUS — no caller can actually produce this state.

Your task: Generate a TIGHTENED precondition that:
1. EXCLUDES the spurious counterexample state
2. STILL ADMITS all states that callers can actually produce
3. Is no more restrictive than necessary

Original precondition:
{original_precondition}

Spurious counterexample state (variable assignments that led to the false alarm):
{spurious_state}

States that callers CAN actually produce (must still be admitted):
{caller_reachable_states}

Refinement iteration: {iteration}

Respond with ONLY valid JSON in this exact format:
{{
  "refined_precondition": "<new tightened precondition>",
  "reasoning": "<why this refinement excludes the spurious state without over-restricting>",
  "excluded_condition": "<the specific condition that excludes the spurious state>"
}}
"""

OVER_REFINEMENT_CHECK_PROMPT = """\
You are a formal verification expert checking if a precondition refinement is safe.

A function precondition was refined. You must determine if the NEW precondition
is still SATISFIABLE by the states that callers can actually produce.

New (refined) precondition:
{new_precondition}

Caller-provided expected preconditions (states callers can produce):
{caller_expected_preconditions}

Question: Does the new precondition EXCLUDE any state that callers can actually produce?
In other words: is there any caller that satisfies its expected precondition but VIOLATES
the new precondition? If so, the refinement is OVER-REFINED and should be rejected.

Respond with ONLY valid JSON in this exact format:
{{
  "is_over_refined": true/false,
  "reasoning": "<explanation of why it is or is not over-refined>",
  "problematic_caller_state": "<if over-refined, describe the caller state that is excluded>"
}}
"""

REPRODUCER_PROMPT = """\
You are a formal verification expert generating C test cases that trigger bugs.

A real bug was found in function '{buggy_function}'. The call chain from the
system entry point to the buggy function is:
{call_chain}

The counterexample (variable state that triggers the bug):
{counterexample_state}

Function signatures:
{function_signatures}

Your task: Generate a minimal, self-contained C test case (main function) that:
1. Creates the necessary data structures
2. Initializes them to concrete values that will trigger the bug
3. Calls the functions in order along the call chain
4. The bug should manifest (crash, assertion failure, or undefined behavior)

The test case should be realistic — use the counterexample values as a guide.

Respond with ONLY valid JSON in this exact format:
{{
  "reproducer_code": "<complete C test case as a string>",
  "explanation": "<brief explanation of why these inputs trigger the bug>",
  "concrete_values": {{
    "<variable>": "<value>",
    ...
  }}
}}
"""

CALLER_HEAVY_SPEC_PROMPT = """\
Generate a specification for this function, emphasizing what CALLERS REQUIRE from it.
Focus on: what preconditions must hold for callers' intended usage, what postconditions
callers depend on. Do not over-specify implementation details.

ALSO emit a ``functional_spec`` field — a Rust/C boolean expression specifying what
a CORRECT implementation would compute. This is orthogonal to the defensive pre/post:
the verifier uses it to check the implementation matches the intended computation.
Examples:
  - For ``read_u16(data, off) -> u16``:
      result == u16::from_le_bytes([data[off], data[off+1]])
  - For ``gnu_hash(name) -> u32`` (djb2-style):
      result == name.iter().fold(5381u32, |h, &b| h.wrapping_mul(33).wrapping_add(b as u32))
  - For ``align_up_64(val, align) -> u64``:
      (result >= val) && (result - val < align) && (result % align == 0)
Tautological-looking specs (reference literally mirrors the body) are CORRECT and
should be emitted — they catch interference / off-by-one bugs in the body. Only
leave empty when the function returns a structured value (AST, hashmap) where
boolean-over-scalars can't express equality.

Function signature (use THESE parameter names in your spec):
{signature}

Caller context (how this function is used):
{caller_context}

Function body:
{body}

Respond with ONLY valid JSON:
{{
  "precondition": "<precondition emphasizing caller requirements>",
  "postcondition": "<postcondition emphasizing what callers depend on>",
  "functional_spec": "<Rust/C boolean expression specifying the intended computation; empty if structured-value return>",
  "reasoning": "<brief explanation>"
}}
"""

IMPL_HEAVY_SPEC_PROMPT = """\
Generate a specification for this function, emphasizing what the IMPLEMENTATION ACTUALLY DOES.
Focus on: what the implementation guarantees based on its code, regardless of caller expectations.

ALSO emit a ``functional_spec`` field — a Rust/C boolean expression specifying what
a correct implementation should compute. Since this prompt emphasises implementation
faithfulness, the functional spec is the *reference computation* against which the
verifier checks the body. Templates:
  - Byte reader: ``result == u16::from_le_bytes([data[off], data[off+1]])``
  - Hash fold:   ``result == name.iter().fold(SEED, |h, &b| h.op(K).op(b as T))``
  - Alignment:   ``(result >= val) && (result - val < align) && (result % align == 0)``
  - Wrapper:     ``result == reference_call(args)``
Tautological reference computations (reference mirrors body) are CORRECT and should
be emitted — they prove the body executes the formula faithfully. Leave empty only
when the function returns a structured value (AST, hashmap) that boolean-over-scalars
can't express.

Function signature (use THESE parameter names in your spec):
{signature}

Function body:
{body}

Respond with ONLY valid JSON:
{{
  "precondition": "<precondition based on what the implementation requires>",
  "postcondition": "<postcondition based on what the implementation guarantees>",
  "functional_spec": "<Rust/C boolean expression specifying the reference computation; empty if not expressible>",
  "reasoning": "<brief explanation>"
}}
"""

SPEC_DISAGREEMENT_PROMPT = """\
You are comparing two candidate specifications for the same C function.

Spec A (caller-emphasis):
  Precondition: {pre_a}
  Postcondition: {post_a}

Spec B (implementation-emphasis):
  Precondition: {pre_b}
  Postcondition: {post_b}

Determine if these specs DISAGREE in a meaningful way that signals ambiguity, a likely bug,
or an under-specified interface. Minor wording differences are not disagreements.
Genuine disagreements are: one allows inputs the other forbids, one guarantees things the
other does not, one implies a correctness property the other is silent on.

Respond with ONLY valid JSON:
{{
  "disagree": true or false,
  "reason": "<brief explanation — what specifically differs and why it matters>"
}}
"""

SPEC_CONSISTENCY_PROMPT = """\
You are checking whether a callee's specification is consistent with how its caller uses it.

Caller function: {caller_name}
Caller spec:
  Precondition: {caller_pre}
  Postcondition: {caller_post}
Caller body (relevant call site):
{call_site}

Callee function: {callee_name}
Callee spec:
  Precondition: {callee_pre}
  Postcondition: {callee_post}

Check: does the caller establish the callee's precondition at the call site? Does the caller
rely on properties from the callee's postcondition that the callee actually guarantees?

Respond with ONLY valid JSON:
{{
  "consistent": true or false,
  "reasoning": "<explanation of any mismatch>"
}}
"""

REALISM_CHECK_PROMPT = """\
You are a formal verification expert auditing a potential bug report for realistic exploitability.

A tool found a property violation in a C function. Your task: determine whether this violation
represents a bug that could occur in the real program, or whether it is a verification artifact
(false positive).

---
FUNCTION UNDER TEST: {function_name}

Signature:
{function_signature}

Body:
{function_body}

---
VIOLATED PROPERTY: {violated_property}

COUNTEREXAMPLE — variable assignments that trigger the violation:
{counterexample_state}

---
CALL CHAIN (how the function is reached from the program entry):
{call_chain}

CALLER CONTEXT — bodies of the immediate callers:
{caller_context}

---
DYNAMIC VALIDATION RESULT: {dynamic_result}

HARNESS CODE (what was actually compiled and run):
{harness_code}

---
CALL-SITE ANALYSIS — how this function is actually called in the codebase:
{call_site_analysis}

GLOBAL VARIABLE CONTEXT — where key globals used by this function are assigned:
{global_context}

---
FULL SOURCE FILE CONTEXT — every function in the same .c file is available below.
You MUST cross-reference any function you cite in your reasoning against this
context. If your call chain depends on a function NOT defined below, you cannot
return REALISTIC.

```c
{source_file_context}
```

---
{threat_model_context}

---
ANALYSIS GUIDANCE

CRITICAL DISTINCTION — witness vs. vulnerability class:
A CBMC counterexample is just ONE witness path to a violation. The specific variable
values in the counterexample may be a CBMC artifact (aliasing that is impossible in
real C, an extreme symbolic value, etc.) while the UNDERLYING VULNERABILITY CLASS
(null dereference, buffer overflow, integer overflow, use-after-free) is real and
triggerable by different inputs.

Ask two separate questions:
  Q1. Could ANY realistic input trigger this TYPE of violation in the real program?
  Q2. Are the specific counterexample witness values achievable in real execution?

Decision rules:
  • Q1=YES, Q2=YES → REALISTIC
  • Q1=YES, Q2=NO  → UNCERTAIN (witness is a CBMC artifact, but bug class is real)
  • Q1=NO,  Q2=any → UNREALISTIC

UNREALISTIC should only be returned when the violation TYPE is impossible in practice
(e.g., pure loop-unwind bound with no real termination issue, violation that requires
a mathematical impossibility, or call-site analysis proves all callers guard the path).
Do NOT return UNREALISTIC merely because the specific witness values are unrealistic.

A finding is UNREALISTIC when:
1. The violation is a loop-unwinding bound (*.unwind.*) AND the loop always terminates
   in real execution for every realistic input — no real infinite-loop scenario exists.
2. The counterexample requires a pointer argument to be NULL, but the call-site analysis
   shows ALL real callers always pass a valid non-NULL pointer with no exception path.
3. The violated postcondition only fails with callee stub return values that are
   mathematically impossible from the real callee (e.g. size_t returning negative).
4. The violation requires multiple simultaneous hardware/callee failures that cannot
   co-occur in any real execution scenario.

A finding is REALISTIC when:
1. The triggering input class could plausibly arise from environment, user, network, or
   hardware (parsers, drivers, OS entry points, functions handling external data).
2. The NULL or overflow occurs on a code path reachable with normal usage AND the
   call-site analysis does not contradict it.
3. The dynamic harness triggered the same fault (confirmed signal in dynamic result).
4. The call chain goes through a system entry that receives untrusted/unvalidated input.
5. The global context shows the variable CAN take the problematic value in practice.
6. Even if this specific CBMC witness is an aliasing artifact or symbolic extreme —
   if the same violation TYPE is reachable via other inputs, lean UNCERTAIN not UNREALISTIC.

EVIDENCE REQUIREMENTS — these are MANDATORY before returning REALISTIC.
Empirically, REALISTIC verdicts are wrong far more often than they're right
unless the LLM (you) commits to specific evidence on the record:

  REQ-1. Cite the specific source-line guard that would have to be
         bypassed for the counterexample to reach the violation. Search
         the FULL SOURCE FILE CONTEXT above (not just the function body
         excerpt) for `if (ptr == NULL) return ...;`,
         `if (len > MAX) return error;`, `DEBUGASSERT(...)`,
         `__CPROVER_assume(...)`, or lazy-init patterns like
         `if (x == NULL) {{ x = alloc(...); }}` BEFORE the violation
         point. Quote the exact line(s) verbatim with line number and
         explain how the witness bypasses them. If you searched the
         full source file and found none, say "no guard found in this
         file" explicitly — but only after a careful read.

  REQ-2. Produce a concrete public-API calling sequence that reaches
         the witness state. Start from a real entry point (a function
         exported in the public header, or a syscall/network handler),
         and show each call in the chain with the argument values that
         lead to the violation. **CRITICAL**: for every function you
         name in the chain, that function MUST appear in the FULL
         SOURCE FILE CONTEXT above. If you reason about what
         `someFunction()` does to global state, you MUST quote the
         relevant lines from its body in the context. If a step in
         your chain depends on a function NOT defined in the
         context (e.g. you assume `helperX()` zeroes a field but its
         body isn't shown), you cannot vote REALISTIC — vote UNCERTAIN
         and note the missing context.

  REQ-3. If the dynamic validation result is "not triggered" or the
         witness involves a CBMC stub return value (`bsearch`,
         `malloc`/`calloc`/family, `strdup`, project-specific allocator
         indirections like `Curl_ccalloc` or `OPENSSL_zalloc`), default
         to UNREALISTIC unless you can show the same fault triggered
         dynamically. CBMC stubs return symbolic / unconstrained values
         that don't match real libc / real allocator behavior.

If you can't fulfill REQ-1 AND REQ-2 with concrete citations and a
real call chain, the verdict is UNREALISTIC. Empty `key_concern` on a
REALISTIC verdict is a contradiction — describe the specific exploit
scenario.

Respond with ONLY valid JSON:
{{
  "verdict": "REALISTIC" | "UNREALISTIC" | "UNCERTAIN",
  "reasoning": "<step-by-step: first answer Q1 (can the violation TYPE occur?), then Q2 (is this witness realistic?), then for REALISTIC verdicts cite REQ-1 source-line guard analysis and REQ-2 public-API call chain>",
  "source_line_guard": "<REQ-1: quote the specific guard or 'no guard found'. REALISTIC requires this is populated.>",
  "public_api_call_chain": "<REQ-2: real entry point → ... → function under test with arguments. REALISTIC requires a concrete chain.>",
  "key_concern": "<the specific scenario that makes this realistic/unrealistic — must be non-empty>",
  "confidence": "high" | "medium" | "low"
}}
"""

REACHABILITY_PROMPT = """\
You are a formal verification expert analyzing C code reachability.

Question: Can the function '{caller_name}' produce a specific variable state
at the point where it calls '{callee_name}'?

Caller function body:
{caller_body}

Target state at call to '{callee_name}' (counterexample variable assignments):
{target_state}

Caller's precondition (what inputs the caller accepts):
{caller_precondition}

Analyze the code carefully:
1. Under what inputs to '{caller_name}' would the variables have the target values
   when '{callee_name}' is called?
2. Are those inputs consistent with the caller's precondition?
3. Is there a code path from the start of '{caller_name}' to the call of '{callee_name}'
   that produces the target state?

Respond with ONLY valid JSON in this exact format:
{{
  "is_reachable": true/false,
  "reasoning": "<step-by-step analysis of reachability>",
  "witnessing_inputs": "<if reachable, describe the concrete inputs that produce the target state>",
  "blocking_condition": "<if not reachable, what condition prevents the target state>"
}}
"""
