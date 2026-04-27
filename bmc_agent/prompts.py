"""
Prompt templates for GRACE Phase 1 spec generation and Phase 3 validation.
"""

from __future__ import annotations

DSL_GRAMMAR = """\
The specification DSL:
- precondition: "requires <formula>"
- postcondition: "ensures <formula>"
- formulas can use: &&, ||, !, forall, exists
- predicates: valid(ptr), in_bounds(arr, idx), locked(lock), owns(ptr), null(ptr)
- arithmetic: +, -, *, /, %, <, <=, >, >=, ==, !=
- you may also write natural language conditions if formal DSL is insufficient
- return value is referred to as \\result
- parameters use their actual names from the function signature
"""

ENTRY_SPEC_PROMPT = """\
You are a formal verification expert generating function specifications for C code.

{dsl_grammar}

Your task: Given a C function's implementation and domain knowledge, generate a precise
formal specification (precondition and postcondition).

The specification should:
- Capture what the function REQUIRES of its inputs (precondition)
- Capture what the function GUARANTEES about its outputs and side effects (postcondition)
- Be tight enough to be useful for verification, not just "true"

Domain knowledge:
{domain_knowledge}

Function signature:
{signature}

Function body:
{body}

Respond with ONLY valid JSON in this exact format:
{{
  "precondition": "<precondition in DSL or natural language>",
  "postcondition": "<postcondition in DSL or natural language>",
  "reasoning": "<brief explanation of why these conditions are correct>"
}}
"""

INTERNAL_SPEC_PROMPT = """\
You are a formal verification expert generating function specifications for C code.

{dsl_grammar}

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

Function signature:
{signature}

Function body:
{body}

Domain knowledge:
{domain_knowledge}

Merging rule for caller specs:
- precondition = disjunction (OR) of callers' expected preconditions
  (function must work if ANY caller's precondition holds)
- postcondition = conjunction (AND) of callers' expected postconditions
  (function must satisfy ALL callers' requirements)

Respond with ONLY valid JSON in this exact format:
{{
  "precondition": "<precondition in DSL or natural language — use the callee's parameter names>",
  "postcondition": "<postcondition in DSL or natural language — use the callee's parameter names>",
  "reasoning": "<brief explanation of why these conditions are correct>"
}}
"""

EXPECTED_SPEC_PROMPT = """\
You are a formal verification expert analyzing C function calls.

{dsl_grammar}

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
You are a formal verification expert generating function specifications for C code.

{dsl_grammar}

Generate a specification for this function, emphasizing what CALLERS REQUIRE from it.
Focus on: what preconditions must hold for callers' intended usage, what postconditions
callers depend on. Do not over-specify implementation details.

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
  "reasoning": "<brief explanation>"
}}
"""

IMPL_HEAVY_SPEC_PROMPT = """\
You are a formal verification expert generating function specifications for C code.

{dsl_grammar}

Generate a specification for this function, emphasizing what the IMPLEMENTATION ACTUALLY DOES.
Focus on: what the implementation guarantees based on its code, regardless of caller expectations.

Function signature (use THESE parameter names in your spec):
{signature}

Function body:
{body}

Respond with ONLY valid JSON:
{{
  "precondition": "<precondition based on what the implementation requires>",
  "postcondition": "<postcondition based on what the implementation guarantees>",
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
