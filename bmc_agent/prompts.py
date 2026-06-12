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
- FULLY PARENTHESIZE any formula mixing && and ||: && binds tighter than ||, so
  "a && b || c" means "(a && b) || c" — write the grouping explicitly. E.g. for a
  max characterization write  result >= x && result >= y && (result == x || result == y)
  NOT  result >= x && result >= y && result == x || result == y
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
  * FULLY PARENTHESIZE any expression that mixes && and ||. In C, &&
    binds tighter than ||, so `a && b || c` means `(a && b) || c` —
    almost never what you intend. Write the grouping explicitly, e.g.
    `a && (b || c)`. For a characterization like max, write
    `result >= x && result >= y && (result == x || result == y)`,
    NOT `result >= x && result >= y && result == x || result == y`.

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

OPTIONAL PRE-CLAUSE SPLIT (internal taxonomy):
The JSON object may include two extra fields, "pre_validity" and
"pre_protocol", that organise the precondition into:

  * pre_validity — caller-establishable memory-safety primitives:
    valid(p), valid_range(buf, 0, n), !null(p), in_bounds(arr, i),
    no_overflow(), sizeof bounds, pointer != NULL, index < length.
  * pre_protocol — higher-level cooperation invariants: locked(L),
    state machine equalities (obj->state == READY), reference counts,
    attached / initialized flags.

Both halves are ASSUMED uniformly in the harness; the split is only an
internal organisation aid (it shapes evidence-tag trust scoring and
gives the feedback loop a basis for which clauses to drop preferentially
when an over-tight inferred clause causes spurious CExs). Either field
may be empty/absent — the harness uses the flat precondition.
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


# Safety-only postcondition clause (M3). Appended to the system prompt
# when Config.safety_only is True. Constrains the LLM's postcondition
# output to memory-safety / range-bound / NaN-Inf-freedom predicates.
# Forbids functional-correctness postconditions whose SMT obligations
# can't be bounded at scale (float associativity, exact algebraic
# equivalence, etc.). The right default for ML / numerics kernels.
SAFETY_ONLY_POSTCOND_CLAUSE = """\

SAFETY-ONLY POSTCONDITION MODE — RESTRICTED GRAMMAR
The postcondition must be a conjunction of clauses drawn ONLY from the
following set. Functional / algebraic / mathematical-correctness clauses
are FORBIDDEN in this mode — they translate to verification obligations
that the SMT solver can't bound at scale for float arithmetic.

Allowed postcondition clauses:
  * !isnan(result), !isinf(result)         — no NaN/Inf propagation
  * result >= L && result <= H            — explicit range bound on a scalar return
  * !isnan(arr[i]), !isinf(arr[i])         — element-wise NaN/Inf-freedom
                                              (for output buffers; i is a bound variable)
  * arr[i] >= L && arr[i] <= H            — element-wise range bound on output buffers
  * result == 0, result != 0              — coarse success/failure indicator
  * result != NULL, result == NULL         — pointer-return validity claim
  * Always-true: postcondition := "true"   — explicit no-claim

EXAMPLES (matching this mode):
  GOOD post: !isnan(result) && result >= 0.0f && result <= 1.0f
  GOOD post: true
  BAD  post: result == compute_reference_value(inp)
  BAD  post: forall i. out[i] == sum_j(inp[i*n + j] * weight[j])
  BAD  post: |out[i] - naive_out[i]| <= eps * |naive_out[i]| + ulp_tol

The precondition is unchanged — strengthen it freely with valid_range,
in_bounds, or arithmetic bounds; only the postcondition is restricted.
Memory safety, OOB, NaN-propagation, and overflow are still checked
mechanically by CBMC's built-in property set, so these restricted
postconditions don't lose coverage of bug classes.
"""


def spec_system_prompt_for(language: str, strict: bool = False, safety_only: bool = False) -> str:
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
        base = RUST_SPEC_SYSTEM_PROMPT
    elif strict:
        base = STRICT_SPEC_SYSTEM_PROMPT
    else:
        base = SPEC_SYSTEM_PROMPT
    if safety_only:
        base = base + SAFETY_ONLY_POSTCOND_CLAUSE
    return base

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
You are generating a C reproducer that demonstrates a bug AGAINST THE REAL
LIBRARY. The reproducer will be compiled and linked against the project's
installed .so / .a; a crash in YOUR fabricated code does NOT validate the
bug, only a crash in the real library code does.

A real bug was found in function '{buggy_function}'. The call chain from the
system entry point to the buggy function is:
{call_chain}

The counterexample (variable state that triggers the bug):
{counterexample_state}

Function signatures (these are real symbols you call — do NOT re-implement them):
{function_signatures}

Your task: emit a minimal C ``main()`` that drives the REAL library to
trigger the bug.

HARD CONSTRAINTS — your reproducer will be REJECTED if it violates any of these:

  1. MUST `#include <archive.h>` (and `<archive_entry.h>` if you use entries).
     The reproducer is compiled with `-larchive` and linked against the real
     project's shared library. Internal headers (`*_private.h`) are NOT on
     the include path — don't reach for them.

  2. MUST NOT re-implement project functions inline. If you redefine
     `entry_list_add`, `add_entry`, `archive_match_*`, etc. in your own C,
     you're testing your own stub, not the real library. The link step
     will use the real symbols regardless of what you define, but any
     "crash" in your inline duplicate is a synthetic FP, not a real bug.

  3. MUST NOT fabricate copies of internal structs (`struct match_file`,
     `struct entry_list`, `struct archive_match`, etc.). These are opaque
     types. Use the public-API constructors (`archive_match_new()`,
     `archive_entry_new()`, …) to obtain instances, and the public setters
     to populate them. Pointer arithmetic on opaque pointers / direct
     field access is forbidden — there is no way to do that from outside
     the library and reach a real bug.

  4. MUST drive state via public-API calls only. To set a struct's
     internal state, you call the public functions that set that state.
     If the counterexample requires a state no public API can produce,
     emit exactly `// UNREPRODUCIBLE: <one-line reason>` and nothing else
     — that's the honest answer, and it's what realism uses to demote
     the finding.

  5. If the function can take attacker-controlled bytes (filename,
     archive contents), construct those as plain `char[]` arrays inline.
     Keep everything in-memory: `archive_read_open_memory`,
     `archive_write_open_memory` (correct signatures: buffer is `void*`
     not `void**`; size is `size_t value` not `&size_t`).

Respond with ONLY valid JSON in this exact format:
{{
  "reproducer_code": "<complete C source — MUST start with #include <archive.h> OR be exactly the UNREPRODUCIBLE comment>",
  "explanation": "<brief explanation of why these inputs trigger the real-library bug>",
  "concrete_values": {{
    "<variable>": "<value>",
    ...
  }}
}}
"""

GENERIC_REPRODUCER_PROMPT = """\
You are generating a C reproducer that demonstrates a bug in a function that
is compiled DIRECTLY into your program (this is internal project code, NOT a
third-party library behind a public API). A crash in YOUR fabricated helper
code does NOT validate the bug — only a crash inside the function under test
does.

A real bug was found in function '{buggy_function}'. The call chain from the
system entry point to the buggy function is:
{call_chain}

The counterexample (variable state that triggers the bug):
{counterexample_state}

Function signatures (these are real symbols compiled WITH your program — call
them directly; do NOT re-implement them):
{function_signatures}

Your task: emit a minimal C ``main()`` that drives the call chain to trigger
the bug at runtime. It is compiled with GCC + AddressSanitizer +
UndefinedBehaviorSanitizer against the project's own source; if the bug is
real it should crash (SIGSEGV / SIGABRT / SIGFPE) or trip an ASan/UBSan report.

HARD CONSTRAINTS — your reproducer will be REJECTED if it violates any of these:

  1. Call ``{buggy_function}`` (and the rest of the chain) DIRECTLY. The
     function, its parameter types, and the project's declarations are
     compiled together with your program. Do NOT assume any specific
     third-party library and do NOT ``#include`` a library public-API header
     such as ``<archive.h>`` — that framing is a stray artifact of an upstream
     step and does NOT apply to this target. Use only the standard headers you
     actually need (``<stdint.h>``, ``<string.h>``, ``<stdlib.h>`` ...) plus
     any project header that genuinely declares the functions you call.

  2. MUST NOT re-implement the project functions inline. The link/compile step
     uses the real symbols; any "crash" in a duplicate you define yourself is a
     synthetic false positive, not a real bug.

  3. Construct the EXACT argument values the counterexample calls for — a
     crafted byte buffer, an out-of-range length / index / offset, a malformed
     string. Define any input bytes inline as a ``char[]`` / ``uint8_t[]``
     array and match each function's parameter order and types precisely.

  4. Wrap the suspect call in a region marked ``// === BUG TRIGGER ===`` so a
     reviewer can navigate to it. Free anything you allocate so LeakSanitizer
     noise doesn't mask the bug.

  5. If the counterexample requires state no caller can actually produce, emit
     exactly ``// UNREPRODUCIBLE: <one-line reason>`` and nothing else — that's
     the honest answer realism uses to demote the finding.

Respond with ONLY valid JSON in this exact format:
{{
  "reproducer_code": "<complete C source — first line must be #include OR exactly the UNREPRODUCIBLE comment>",
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
A bounded-model-checker (CBMC) just flagged this code as containing a
potential bug. **CBMC is wrong far more often than it is right.** Your
job is the same job a careful security auditor or pen tester would do
if they got this report: look at it critically, treat the bug claim
as suspect, and identify whether it's a real exploitable bug or one
of the many failure modes that produce false positives.

DEFAULT POSITION: TREAT THE BUG CLAIM AS SUSPECT
=================================================
Start by assuming this is probably a false positive, because most
CBMC findings on real-world C code are. The common false-positive
patterns you should rule out FIRST:

  1. **Unreachable-from-any-entry-point state**: CBMC's harness uses
     nondeterministic inputs, which gives it the ability to construct
     struct states no real caller can produce. Before voting
     REALISTIC, you must identify the SPECIFIC entry point an attacker
     controls — appropriate to THIS codebase per the threat model below
     (a library public-API call, a syscall/trap argument, a network
     packet or file/image/format the code parses, a device/MMIO/DMA
     interface) — AND the inputs to it that produce the state the
     counterexample requires. If you can only say "an attacker could
     somehow corrupt this field," it's not enough — name the entry point.

  2. **Stub-callee disconnect**: CBMC replaces external calls with
     stubs that return arbitrary values. If the bug requires the
     stub to return something the real callee cannot return (e.g.
     `malloc` returning a 2-byte buffer that the code expected to be
     bigger; a length/size accessor returning 2 when its own logic
     guarantees >= 31), it is NOT a real bug.

  3. **Witness-uses-uninitialized-state**: CBMC may exhibit a witness
     where a pointer is non-NULL but points to deallocated memory, or
     a field has a stack-address value. If no public-API sequence
     produces that exact state (free-without-null, write-then-free
     out of order, etc.), it is NOT a real bug — even if the function
     under test lacks the guard that would prevent it.

  4. **Direct-NULL / direct-misuse of trivial helpers**: Tiny utility
     functions (byte encoders/decoders, format helpers) trivially
     crash if you pass NULL or invalid args, but real callers never
     do. The bug isn't in the helper — it's in whatever passes bad
     args, which must itself be reachable from an attacker-controlled
     entry point.

  5. **Theoretical overflow without practical input**: A `size_t`
     overflow that requires combining inputs whose product exceeds
     the address space is not exploitable in practice.

For your verdict to be REALISTIC, you must be able to answer YES to
ALL of these:

  (a) Can you name a specific entry point an attacker controls,
      appropriate to THIS codebase per the threat model below (a
      library public-API call, a syscall/trap argument, a network
      packet or file/image/format the code parses, a device/MMIO
      interface)?
  (b) Can you describe what bytes/values the attacker supplies (file or
      packet bytes, header fields, argument values) to that entry point?
  (c) Walking from that entry point through the codebase, can you
      explain how those bytes turn into the counterexample's
      precondition state?
  (d) Does the bug class survive WITHIN the realistic input domain
      (not just with CBMC's symbolic-extreme values)?

If you can't answer ALL FOUR with concrete code-level evidence, vote
UNREALISTIC. "It looks like a potentially exploitable pattern"
without a reachable path is exactly the false-positive shape we are
trying to avoid.

UNCERTAIN is for cases where you can answer some but not all of
(a)-(d), and where the gaps are because you genuinely don't have
enough information (e.g. an external caller that isn't in this file
might supply the input). UNCERTAIN is NOT a polite version of
REALISTIC; it's a request for more context.

ONE MORE THING: hardening-only findings (theoretical overflows,
missing-guard claims for states no caller produces, "should defend
against this even though it's not currently triggerable") are NOT
real bugs for this assessment. They might be worth defensive
hardening but they are not exploitable defects.

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

CBMC HARNESS — the actual harness whose initial state produced the counterexample.
Audit it directly: this is the most reliable signal for harness-artifact FPs.
Look for: pointers initialized to NULL/stack/uninitialized, struct fields set
nondeterministically without honoring the invariants a real caller establishes,
freed-without-NULL state, integer fields set to extreme values that real calling
code would not produce. If the harness's initial state cannot be produced by any
attacker-reachable call sequence, the counterexample is a harness artifact
regardless of how realistic the function-level pattern looks.
{harness_code}

---
CALL-SITE ANALYSIS — how this function is actually called in the codebase:
{call_site_analysis}

GLOBAL VARIABLE CONTEXT — where key globals used by this function are assigned:
{global_context}

---
ACTIVE STUB CONTRACTS — library-level guarantees that real callees honor.

The harness replaces calls to external functions with stubs. Each stub was
generated WITH the documented library contract for that function — meaning
the stub's nondet output is CONSTRAINED to obey what real callees actually
return. If the counterexample witness state requires any of these
contracts to be VIOLATED, the violation is unreachable from any real
caller — because no real implementation of the library produces that
output. Treat it as a stub-callee-disconnect FP and return UNREALISTIC.

Contracts active for callees of {function_name}:
{active_stub_contracts}

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
YOUR TASK — SELF-DIALOGUE AUDIT
================================
A careful security auditor doesn't classify a finding in one shot.
They reason adversarially across multiple internal turns, challenging
their own first impression. Simulate that internal dialogue. For
EACH of the five turns below, write the reasoning BEFORE moving to
the next turn. Each turn's reasoning is input to the next.

TURN 1 — FIRST READ (no skepticism yet)
  What is the bug class CBMC claims? What's the apparent failure
  pattern? At first glance, does it look like a known-real bug
  pattern (e.g. OOB read with attacker-controlled length, double
  free, UAF, integer overflow propagating to allocation)? Just
  pattern-match. Don't audit yet.

TURN 2 — REACHABILITY CHALLENGE
  Now play the skeptic. Find an entry point an attacker controls (per
  the threat model below — a library public-API call, a syscall/trap
  argument, a network packet or file/image/format the code parses, a
  device/MMIO interface) which (directly or transitively) invokes
  `{function_name}`. You need to commit to one of three answers:

  (a) BEHAVIORAL REACHABILITY (sufficient for REALISTIC):
      You can identify an attacker-controlled entry that calls this
      function with attacker-controllable input AND you can describe a
      behavioral pattern in this function that misbehaves under
      hostile inputs (missing guard, length-based parser without
      bounds check, type confusion on attacker-typed field, etc).
      You do NOT need to fully byte-trace from file-format bytes
      to the counterexample's exact precondition values — that
      level of trace is rarely possible without active code
      search. Documented seed bugs (next_field-class OOB reads
      on length-based parsers, off-by-one in size calculators
      called from public format-text APIs) live here.

  (b) STRUCTURAL UNREACHABILITY (forces UNREALISTIC):
      The counterexample requires a struct state that NO
      sequence of public-API calls can produce — for example,
      a pointer that is freed-but-not-nulled when the only free
      site in the codebase nulls immediately after. This is the
      classic harness-artifact pattern.

  (c) GENUINELY UNCERTAIN:
      You can't tell whether (a) or (b) holds because the
      reachability depends on a function whose body isn't in the
      provided context, and the inference isn't obvious from
      naming or comments. Vote UNCERTAIN, naming what's missing.

  Don't conflate "I haven't traced every byte" with "not
  reachable." Behavioral reasoning about parser/decoder patterns
  is valid security-audit reasoning.

TURN 2.5 — HARNESS INITIAL-STATE AUDIT (NEW, REQUIRED)
  Read the CBMC HARNESS section above carefully. The harness is the
  code that set up the variables before {function_name} ran. Audit
  its initial-state setup against the public-API invariants:

  Q1: Does the harness call public-API functions to construct the
      input struct(s), or does it use ``__VERIFIER_nondet_*`` /
      direct field writes / arbitrary memory layouts?
  Q2: For every struct field that appears in the counterexample
      witness, ask: does a public-API call sequence ever produce
      that field value? Specific FP patterns to catch:
        - pointer field non-NULL but pointing at a stack address
          ("&stack_buf") or freed memory — public APIs never leak
          stack/freed pointers into struct fields
        - "freed but not nulled" state — if the codebase's free
          paths null the pointer immediately after free, this state
          is unreachable
        - integer field with an impossible value (negative length,
          a length that violates a documented invariant like "always
          ≥ N", an index outside the allocated bound)
        - arbitrary 2-3 byte buffers ("char buf[2]") passed where
          callers always pass full structs
        - the harness skips initializing a field that public-API
          construction always sets (e.g. magic number, type tag)
        - PAIRED FIELDS (count, array_pointer): when a struct has a
          (count, pointer) pair and the witness has count > 0 with
          pointer == NULL, look in the codebase for a sibling
          add_*/append_*/*_init function that maintains the invariant
          "set pointer before incrementing count". The libarchive
          archive_match add_owner_id pattern (count incremented AFTER
          realloc sets ids->ids) is the canonical example. Witness
          state count>0+pointer==NULL is unreachable from public-API
          construction → UNREALISTIC.
  Q3: Is the trigger sequence (e.g. "free X then read X") something
      a public API can produce, or only the harness's nondet writes?

  If Q1 shows the harness shortcuts public-API construction AND Q2
  surfaces any field whose witness value is unreachable from public-
  API state, this is a HARNESS-ARTIFACT FP. Vote UNREALISTIC, citing
  the specific harness line(s) and the public-API invariant that
  rules out the witness state. This Turn catches the FP class that
  Turn 3's stub-callee analysis misses: invalid INPUTS to the
  function-under-test (vs. invalid OUTPUTS from its callees).

TURN 3 — STUB-CALLEE CHALLENGE
  Now check for the CBMC harness artifact pattern: does the
  counterexample require any function in the codebase to return a
  value its real implementation cannot return? Examples: malloc
  returning a 2-byte buffer when the caller computed the size,
  a length/size accessor returning a length its own logic guarantees
  is >= 31, a stub returning a stack address. If yes, that's a
  CBMC-harness artifact — the real callee would never produce that
  return.

TURN 4 — BUG-CLASS REALISM (Q1/Q2)
  CBMC routinely picks SIZE_MAX / 18-exabyte / near-MAX values
  that no real input has. That alone does NOT mean the bug is
  unreachable — the bug class is real if any plausible
  attacker-supplied input in the bug-triggering RANGE produces
  the violation. Q1: is the bug TYPE (OOB read, NULL deref, etc.)
  reachable with realistic inputs? Q2: is the SPECIFIC CBMC
  witness achievable? If Q1=yes and Q2=no, the bug is still real
  (REALISTIC); the witness is just CBMC's artifact. If Q1=no
  (e.g. the bug requires impossible state with no upstream path),
  it's UNREALISTIC.

TURN 5 — FINAL VERDICT
  Combining turns 1-4:
  - REALISTIC if turn 2 reached BEHAVIORAL reachability (case a)
    AND turn 2.5 found no harness-artifact in the initial state
    AND turn 3 did NOT surface a stub disconnect
    AND turn 4 Q1 passed.
  - UNREALISTIC if ANY of these holds:
    * turn 2 reached STRUCTURAL unreachability (case b);
    * turn 2.5 surfaced a harness-artifact in the initial state
      (impossible struct field values for public-API construction);
    * turn 3 surfaced a stub disconnect;
    * turn 4 Q1 failed (the bug type is mathematically impossible
      with realistic input).
  - UNCERTAIN if turn 2 was case (c) — genuinely missing info,
    not just "I haven't grep'd the whole codebase."

  HARDENING-ONLY findings (theoretical overflow, missing guard for
  a state no caller produces) are NOT realistic. They might be
  worth defensive hardening but they are not exploitable defects.

OUTPUT FORMAT — JSON, all turns visible
{{
  "turn_1_first_read": "<pattern matched>",
  "turn_2_reachability": "<public-API entry + behavioral pattern (case a), structural unreachability (case b), or genuinely uncertain (case c)>",
  "turn_2_5_harness_initial_state": "<Q1: harness uses public-API construction OR nondet/direct field writes? Q2: any field value in witness that public-API state cannot produce (cite specific harness line + public-API invariant)? Q3: trigger sequence achievable from public API?>",
  "turn_3_stub_disconnect": "<yes + specific clause violated, OR 'no disconnect'>",
  "turn_4_q1_q2": "<Q1 result + reason, Q2 result + reason>",
  "verdict": "REALISTIC" | "UNREALISTIC" | "UNCERTAIN",
  "reasoning": "<one-paragraph synthesis of turns 1-5>",
  "exploit_scenario": "<for REALISTIC: the specific public-API call sequence + attacker bytes. For UNREALISTIC: which turn failed and why. For UNCERTAIN: what info you need>",
  "confidence": "high" | "medium" | "low"
}}
"""


# Adjacent-bug discovery prompt. Fired as a SECOND, independent LLM call
# after the primary realism check. Kept separate so the auditor's responsibility
# split doesn't dilute the primary verdict's reasoning budget or prime the LLM
# toward "wrong location" thinking on the main CBMC finding.
ADJACENT_BUG_PROMPT = """\
You are a senior C security auditor. Below is a function from a real C codebase,
its callers, callees, struct definitions, and a CBMC counterexample for a
property the bounded-model-checker flagged in this function.

A separate realism check has already judged the CBMC counterexample itself.
Your task is DIFFERENT: independently scan the FUNCTION UNDER TEST, its
callers, callees, and the surrounding source for OTHER exploitable defects
that the CBMC finding did NOT capture.

A defect is exploitable if an attacker who controls external input (file
bytes, network data, malformed archive, hostile API call sequence) can reach
a state that crashes, corrupts memory, leaks data, or otherwise violates a
safety/security property.

EXAMPLES of patterns worth reporting:
- Partial-init / error-rollback paths that leave a struct half-initialized,
  so a later cleanup or use operates on inconsistent state.
- Public-API call sequences (double-init, call-after-cleanup, out-of-order)
  the implementation doesn't handle defensively.
- Untrusted input fields controlling a size/index that bypasses a check.
- A nearby function in the same file has the same vulnerability pattern.
- Stub-callee contract holes (the function trusts a callee's return without
  bounds-checking).

Be precise: only list defects you can describe with concrete code evidence
(specific function + approximate line + attacker scenario). Don't list
hypotheticals. An empty list is honest if you see none.

---
FUNCTION UNDER TEST: {function_name}

Signature:
{function_signature}

Body:
{function_body}

---
VIOLATED PROPERTY (CBMC primary finding — for your reference only;
NOT the bug you're hunting): {violated_property}

COUNTEREXAMPLE STATE (CBMC primary finding):
{counterexample_state}

---
CALL CHAIN: {call_chain}

CALLER CONTEXT:
{caller_context}

CALL-SITE ANALYSIS:
{call_site_analysis}

ACTIVE STUB CONTRACTS:
{active_stub_contracts}

---
FULL SOURCE FILE:
```c
{source_file_context}
```

---
Respond with ONLY valid JSON:
{{
  "adjacent_bugs": [
    {{
      "location": "<function_name or function_name:line>",
      "bug_type": "<NULL deref / OOB read / partial-init UAF / double-free / call-after-free / etc.>",
      "attacker_scenario": "<one paragraph: what input or call sequence reaches it, citing specific lines>",
      "confidence": "high" | "medium" | "low"
    }}
  ]
}}

If you see no adjacent bugs, respond with `{{"adjacent_bugs": []}}`.
"""

# ---------------------------------------------------------------------------
# Caller-grounded spec generation (v2)
# ---------------------------------------------------------------------------
#
# Used by SpecGeneratorV2. Distinct from the v1 ENTRY_SPEC_PROMPT in that
# it explicitly hands the LLM independent evidence sources (callers, doc
# annotations, signature-pattern seeds) and asks it to reconcile rather
# than infer from the body alone.
#
# Key design choices baked into this prompt:
#   * Structured JSON output with per-clause evidence tags. The orchestrator
#     enforces "every clause must have ≥1 evidence tag" — the schema is
#     load-bearing for the feedback loop's directed relaxation.
#   * Explicit validity vs protocol split done by the LLM (richer context
#     than the regex classifier in spec.py).
#   * Bias toward weaker PRE when body and callers disagree, with
#     spec_disagreement=True flagged so the orchestrator can mark the
#     finding for human review.

CONTRACT_PRECONDITION_PROMPT = """\
Your task: derive ONLY the PRECONDITION (the caller's obligation) for a single C
function that will be checked by a bounded model checker. The precondition is
what the checker ASSUMES about inputs, so it must be the function's TOLERANCE
CONTRACT — the WEAKEST condition the function genuinely requires for memory
safety — NOT a description of what the current callers happen to pass.

Function under spec:
```c
{fn_signature}
```
Function body:
```c
{fn_body}
```
{callers_block}
POLICY (emit the union of ALL reachable inputs, not the intersection of current
callers):

  * KEEP a clause ONLY if it is a structural memory-safety obligation that holds
    for EVERY caller and that the function does not itself establish:
      - pointer validity: valid(p) / !null(p) for pointers the body dereferences
      - buffer extent the body reads/writes: valid_range(buf, 0, len)
      - non-negativity of a size/length the body uses as an extent: len >= 0
    Use the observed call sites only to CONFIRM such a clause is universal.

  * DROP every constraint on the VALUES of attacker-controlled data — the bytes
    or fields the function reads from its input — EVEN IF all observed callers
    satisfy it. Examples to DROP: `buf[0] < 16`, `data[i] <= MAX`, a data-derived
    `start < end`, magic-number equalities on input bytes, enum-range limits on
    a parsed field. The function must tolerate untrusted / other / future
    callers; assuming these away hides real bugs.

  * If you cannot tell whether a clause is a universal structural obligation or a
    coincidental data constraint, DROP it (keep that input free).

Express clauses in the same DSL the spec generator uses (valid, valid_string,
valid_range, in_bounds, null, owns, and C comparisons). Combine with `&&`.

Output ONLY a JSON object:
{{"pre_validity": "<&&-joined DSL clauses, or \\"true\\" if none>",
  "reasoning": "<for each clause: kept (structural-universal) or dropped (data value) and why>"}}
"""


CALLER_GROUNDED_SPEC_PROMPT = """\
Your task: draft a precise formal spec (precondition + postcondition) for a
single C function, by RECONCILING three independent evidence sources:

  (a) the function body itself (what the code does — may include bugs);
  (b) up to {n_callers} actual call sites in the codebase (what callers
      establish before calling — this constrains what's REALISTIC);
  (c) signature-pattern seeds + author doc annotations (what the
      function declared as).

The reconciliation step is the whole point. A spec derived from (a) alone
captures whatever the buggy code happens to tolerate — that's the failure
mode we are explicitly defending against. Use (b) and (c) as the
EPISTEMIC ANCHOR; use (a) only to refine.

Function under spec:
```c
{fn_signature}
```

{fn_body_block}
{field_accesses_block}
{doc_annotations_block}
{seed_clauses_block}
{callers_block}
{address_taken_block}
{callee_specs_block}

OUTPUT FORMAT — emit ONLY this JSON object, no prose:

{{
  "pre_validity": [
    {{"clause": "<DSL clause>", "evidence": ["caller_site_1", "body:L42"]}}
  ],
  "pre_protocol": [
    {{"clause": "<DSL clause>", "evidence": ["header_comment"]}}
  ],
  "postcondition": [
    {{"clause": "<DSL clause>", "evidence": ["body:L88"]}}
  ],
  "loop_invariants": [],
  "spec_disagreement": false,
  "uncertainty_notes": "<one-line free-form on what you were unsure of>"
}}

EVIDENCE TAG VOCABULARY (use these EXACT strings):
  "body:L<n>"            — derived from reading function body line <n>
  "caller_site_<idx>"    — derived from observing the indexed caller above
                            (1-indexed; e.g. caller_site_1 = first listed)
  "address_taken_site_<idx>" — derived from the indexed vtable/callback
                            registration site (when function only reached
                            via function-pointer dispatch)
  "header_comment"       — extracted from doxygen/header annotation
  "signature_pattern"    — derived from universal pattern matching on
                            parameter names + types (paired_pointers, etc.)
  "canonical_contract"   — derived from a hand-curated registry entry
                            (caller almost never produces this; the
                            orchestrator short-circuits before the LLM call)

HARD RULES — your output will be rejected if any of these are violated:

  1. EVERY clause MUST have ≥1 evidence tag. Untagged clauses are guesses
     and we reject them at parse time.
  2. POST clauses MAY reference ONLY return value, out-parameters, or
     globals visible at the signature scope. Internal struct fields the
     caller cannot observe are forbidden in POST.
  3. When body and callers disagree on a precondition strength, prefer
     the WEAKER PRE (let any caller-side bug surface) and set
     spec_disagreement=true with a uncertainty_notes explanation. Choosing
     the tighter PRE to make the function verify clean is exactly the
     methodology trap we are defending against.
  4. Default ambiguous PRE clauses to pre_validity (caller's obligation)
     rather than pre_protocol (assumed). Asserting too much surfaces
     visible FPs that the feedback loop can drop; assuming too much hides
     bugs invisibly.
  5. If the callers list is EMPTY and address_taken_sites is also empty,
     the function may be dead code or reachable only through paths not
     visible in the corpus. Emit pre/post as trivial ({{}}) with evidence
     ["signature_pattern"] (if seeds exist) or [] (if no signal at all);
     set uncertainty_notes="no caller evidence available".
  6. FIELD-LEVEL GUARDS. For every entry in the "Field accesses to guard"
     block, emit a corresponding `!null(<path>)` clause in pre_validity
     unless one of the following holds:
       * the access is marked `guarded` (body already NULL-checks before
         dereferencing — read the body to confirm)
       * the body proves at the access site that the field is non-NULL
         via some other means (e.g., it was just assigned a non-NULL
         allocation result)
       * the field type is a primitive (not a pointer) — pointer guards
         only apply to pointer-typed fields, which the body's deref tells
         you about
     Multi-hop chains: if `a->b->c` is accessed, you MUST emit guards
     for every prefix that is itself a pointer: `!null(a->b)` AND
     `!null(a->b->c)`. Use evidence tag `body:L<n>` from the hint plus
     `caller_site_<idx>` if a caller establishes the same field.
     Skipping field guards is the canonical cause of pointer_dereference
     CEx storms — be exhaustive here.

VALIDITY vs PROTOCOL — split your PRE clauses:

  pre_validity: caller-establishable memory-safety primitives. Things any
    sane caller is obliged to ensure: !null(p), valid(p), valid_range(p, 0, n),
    in_bounds(idx, arr_len), no_overflow(a + b), arithmetic comparisons of
    pointer/length-shaped values.

  pre_protocol: higher-level cooperation invariants the callee assumes the
    broader system maintains: locked(&mu), initialized(obj), state(s == OPEN),
    ref-count predicates, "callback registered" invariants.

When in doubt → validity. (See rule 4.)

DSL syntax — keep clauses minimal first-order predicates joined implicitly
across the list (each clause is its own AND-conjunct). Allowed primitives:
  !null(p)              — p is non-NULL
  valid(p)              — p points to an allocation
  valid_range(p, lo, hi) — p[lo..hi) is in-bounds for p's allocation
  valid_string(s)       — s is non-NULL and NUL-terminated within bounds
  in_bounds(i, n)       — 0 <= i < n
  no_overflow(expr)     — the arithmetic expr doesn't overflow
  <relational op on params/return>  — e.g. ``start <= end``, ``result >= 0``
"""


def render_caller_grounded_spec_prompt(
    *,
    fn_signature: str,
    fn_body: str,
    callers: list,                # list[CallerEvidence], typed loosely to avoid import cycle
    address_taken_sites: list,
    doc_annotations: list,
    seed_clauses: list,
    field_accesses: list,         # list[FieldAccessHint]
    callee_specs: dict,           # {name: Spec.to_dict()}
    n_callers_actual: int = 5,
) -> str:
    """Render :data:`CALLER_GROUNDED_SPEC_PROMPT` with the evidence bundle.

    Conditional blocks (callers, address-taken, doc annotations, seed
    clauses, callee specs) are omitted entirely when empty so the LLM
    doesn't see boilerplate-with-zero-content. Each block carries its
    own header and the indexing matches the evidence-tag vocabulary.
    """
    fn_body_block = (
        f"Function body:\n```c\n{fn_body}\n```\n" if fn_body.strip() else ""
    )
    if doc_annotations:
        lines = ["Author documentation annotations:"]
        for d in doc_annotations:
            lines.append(f"  - {d.render()}")
        doc_annotations_block = "\n".join(lines) + "\n"
    else:
        doc_annotations_block = ""
    if seed_clauses:
        lines = ["Signature-pattern seed clauses (deterministic, no LLM):"]
        for s in seed_clauses:
            lines.append(f"  - {s.render()}")
        seed_clauses_block = "\n".join(lines) + "\n"
    else:
        seed_clauses_block = ""
    if field_accesses:
        # Dedup by path so the block isn't dominated by repeated accesses,
        # but preserve the line offset of the FIRST occurrence so the LLM
        # can cite body:L<n> in evidence tags.
        seen: set[str] = set()
        ordered: list = []
        for h in field_accesses:
            if h.path in seen:
                continue
            seen.add(h.path)
            ordered.append(h)
        lines = [
            f"Field accesses to guard ({len(ordered)} unique chain(s) "
            f"dereferenced through parameters; emit !null clauses for "
            f"unguarded ones — see RULE 6):"
        ]
        for h in ordered:
            lines.append(f"  - {h.render()}")
        field_accesses_block = "\n".join(lines) + "\n"
    else:
        field_accesses_block = ""
    if callers:
        lines = [f"Observed call sites ({len(callers)} of {n_callers_actual} max):"]
        for i, c in enumerate(callers, start=1):
            lines.append(f"\n--- caller_site_{i} ---")
            lines.append(c.render())
        callers_block = "\n".join(lines) + "\n"
    else:
        callers_block = "Observed call sites: NONE (no direct callers found in corpus).\n"
    if address_taken_sites:
        lines = [
            f"Address-taken / callback-registration sites "
            f"({len(address_taken_sites)} — function only reached via "
            f"function-pointer dispatch):"
        ]
        for i, c in enumerate(address_taken_sites, start=1):
            lines.append(f"\n--- address_taken_site_{i} ---")
            lines.append(c.render())
        address_taken_block = "\n".join(lines) + "\n"
    else:
        address_taken_block = ""
    if callee_specs:
        lines = ["Callees' specs (use as compositional context):"]
        for name, spec in callee_specs.items():
            pre = spec.get("precondition", "") or "true"
            post = spec.get("postcondition", "") or "true"
            lines.append(f"  {name}: PRE={pre!s}  POST={post!s}")
        callee_specs_block = "\n".join(lines) + "\n"
    else:
        callee_specs_block = ""

    return CALLER_GROUNDED_SPEC_PROMPT.format(
        n_callers=n_callers_actual,
        fn_signature=fn_signature,
        fn_body_block=fn_body_block,
        field_accesses_block=field_accesses_block,
        doc_annotations_block=doc_annotations_block,
        seed_clauses_block=seed_clauses_block,
        callers_block=callers_block,
        address_taken_block=address_taken_block,
        callee_specs_block=callee_specs_block,
    )


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
