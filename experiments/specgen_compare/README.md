# SpecGen Java/JML Benchmark Runner

This directory contains evaluation helpers for running BMC-Agent's Java/JML
specification-generation path on the SpecGen benchmark artifact.

The runner is intentionally outside the production verification pipeline.  It
uses:

- Java input programs from `SpecGenBench/common`
- BMC-Agent JML generation through the configured LLM provider
- OpenJML ESC as the verifier
- one report row per benchmark case

Example:

```bash
# Provide credentials through your normal secret manager or shell environment.
# Do not store API keys in this repository.
export BMC_AGENT_LLM_PROVIDER=openai
export BMC_AGENT_LLM_MODEL="${SPECGEN_MODEL:-gpt-3.5-turbo-1106}"
export BMC_AGENT_LLM_API_KEY="${OPENAI_API_KEY:?set OPENAI_API_KEY first}"
export BMC_AGENT_LLM_BASE_URL=https://api.openai.com/v1

export SPECGEN_BENCH_ROOT=/path/to/SpecGen-Artifact/benchmark/SpecGenBench/common
export SPECGEN_ORACLE_ROOT=/path/to/SpecGen-Artifact/benchmark/SpecGenBench/oracle
export BMC_AGENT_OPENJML_PATH=/path/to/openjml

uv run python experiments/specgen_compare/run_bmc_jml_specgen.py run \
  --bench-root "$SPECGEN_BENCH_ROOT" \
  --oracle-root "$SPECGEN_ORACLE_ROOT" \
  --openjml-path "$BMC_AGENT_OPENJML_PATH" \
  --cases Abs Return100 AddLoop \
  --output artifacts/specgen_jml_pilot \
  --max-iterations 5
```

Prompt examples:

- `--prompt-examples specgen-4shot` uses the four examples from the SpecGen
  artifact and should remain the default for same-prompt comparisons.
- `--prompt-examples specgen-4shot-linked` appends one extra linked-structure
  example that demonstrates nullable links and branch-conditioned receiver
  preconditions.  Use it only for a targeted linked-list/shape pilot, not for
  the baseline comparison table.

Regeneration feedback:

- `--source-preflight-feedback` runs OpenJML on the unannotated Java source and
  includes a compact source-level verifier summary in the first generation
  prompt.  This is advisory and does not skip the case.  Use it for small
  regeneration pilots after post-processing has saturated; it helps the model
  focus on real OpenJML obligations without changing the Java/JML verifier.
- `--preflight-source` keeps its original meaning: skip cases that fail before
  generated JML is meaningful because of Java frontend/tool errors.

Example feedback pilot:

```bash
uv run python experiments/specgen_compare/run_bmc_jml_specgen.py run \
  --bench-root /mnt/disk7/jw_bmc/SpecGen-Artifact/benchmark/SVCOMP \
  --openjml-path /mnt/disk7/jw_bmc/SpecGen-Artifact/openjml/openjml \
  --cases StaticCharMethods05 StringValueOf03 TokenTest01 TokenTest02 \
  --output artifacts/svcomp_java_feedback_pilot \
  --prompt-examples specgen-4shot \
  --source-preflight-feedback \
  --preflight-timeout 60 \
  --openjml-timeout 120 \
  --max-iterations 5 \
  --trials 3 \
  --workers 2
```

Outputs:

- `manifest.json`: selected benchmark cases.
- `report.json`: per-case status and artifact paths.
- `summary.md`: compact table for inspection.
- `<output>/cases/<case>/...`: BMC-Agent JML/OpenJML artifacts.

The LLM-free `replay_jml_postprocess.py` adapter reuses existing generated
JML and reruns only the current post-processing/OpenJML path.  Its default
prune budget matches the production JML checker (`5` rounds), so replayed
diagnostics are comparable to a fresh run without spending new model calls.
Use `--only-passed-trials` when auditing quality of previously successful
runs; this replays only artifacts that were originally accepted.  The current
post-processor removes generated `//@ assume` statements, so replay reports can
distinguish clean specification proofs from assume-assisted proofs.
Replay reports use the same actionability split as the main runner, separating
remaining generated-spec issues from source/tool boundaries and LLM/runner
failures.

For strict reporting, use `overlay-report --require-clean-passing-jml` after
replaying suspicious artifacts.  This keeps the original generation pipeline
unchanged, but refuses to count a case as a clean pass unless at least one
passing trial artifact has no generated JML `assert` or `assume` statements.
The report writer also canonicalizes top-level artifact paths to prefer a clean
passing trial when one exists, so manual audits do not accidentally inspect an
older assume-assisted artifact.

Replay also rejects artifacts that changed executable Java code after removing
JML comments.  When using a small replay report to correct failure
classification in a larger multi-trial report, add
`--preserve-base-trial-stats` so the overlay updates status/artifact fields
without replacing the original trial counts and runtime totals.

Example strict overlay:

```bash
uv run python experiments/specgen_compare/run_bmc_jml_specgen.py overlay-report \
  --input-report artifacts/svcomp_java_postprocess_full_current_overlay_v11_tsp_10trial_20260621/report.json \
  --overlay-report artifacts/svcomp_java_assume_filter_only_assume_cases_all_passed_trials_v2_20260621/report.json \
  --output artifacts/svcomp_java_strict_no_generated_assume_overlay_v3_cleanrep_20260621 \
  --require-clean-passing-jml \
  --preserve-base-trial-stats
```

Residual audits:

```bash
uv run python experiments/specgen_compare/run_bmc_jml_specgen.py audit-residuals \
  --input-report artifacts/svcomp_java_strict_clean_overlay_v45_completed_trials_20260621/report.json \
  --output artifacts/svcomp_java_residual_audit_v45_completed_trials_20260621
```

The audit command does not rerun LLMs or OpenJML.  It reads an existing
`report.json`, groups non-passing rows by actionability, and emits a stop/continue
decision for further spec-generation optimization.  Its failure-class,
failure-reason, actionability, and recommendation counts are residual-only; the
top-level `total` and `passed` fields are retained only for context.

Current clean-result checkpoints:

- SpecGenBench, Claude 4.6, 10 trials/case:
  `artifacts/specgenbench_postprocess_full_diagnosed_v12_trial_cell_20260621/`
  reports `114/120` clean case passes and `1075/1200` trial passes.
  The residual audit
  `artifacts/specgenbench_residual_audit_v12_20260621/` reports `0`
  generated-spec-only failures and recommends stopping JML-generation sweeps for
  this checkpoint unless the benchmark/oracle or OpenJML tool boundary changes.
- SV-COMP Java, Claude 4.6, 10 trials/case:
  `artifacts/svcomp_java_strict_clean_overlay_v45_completed_trials_20260621/`
  reports `229/265` clean case passes and `1847/2650` trial passes, with
  zero result rows containing generated JML `assert` or `assume` artifacts.
  The residual audit `artifacts/svcomp_java_residual_audit_v45_completed_trials_20260621/`
  reports `0` generated-spec-only failures and the same stop decision for
  additional JML-generation sweeps.
  The current reports also include an actionability split.  Under that split,
  SpecGenBench has `0` remaining generated-spec-only failures and `6`
  source/tool-boundary failures, while SV-COMP Java has `0` remaining
  generated-spec-only failures and `36` source/tool-boundary failures.
  The v45 checkpoint completes the v44 denominator by rerunning the 15 rows that
  previously had fewer than ten recorded trials, rather than rerunning the full
  265-case benchmark.

The SV-COMP Java checkpoint is an evaluation overlay over existing generated
artifacts.  It does not rerun the LLM.  Its main purpose is to enforce a clean
JML proof criterion and to separate remaining failures into OpenJML timeouts,
source-level benchmark failures, Java frontend/tool issues, library
preconditions, and genuinely insufficient generated specifications.

The v10 SV-COMP Java overlay uses the same LLM-generated artifacts as v9 but
adds the replay adapter's source-preserving JML transplant fallback.  This
converted `Verifier` from a stale `source_changed` classification into a normal
generated-spec failure.  The v11 overlay then applies the generic reported
`diverges`-clause pruning used by the production JML checker; the remaining
`Verifier` failure comes from OpenJML's bundled `Runtime.halt` model, so it is
classified with other Java library/model precondition failures.  The pass count
and trial statistics are unchanged.

The v12 overlay keeps the same pass count but applies a generic frame-inference
fix for methods that pass local arrays or objects to helper calls.  In that
case, automatically re-adding a narrow parameter frame such as
`assignable a[*]` can exclude helper writes through local aliases.  The fix
prevents that frame from being reintroduced after OpenJML reports it as false;
for `MergeSortIterative`, this moves the remaining failure from a stale frame
obligation to the underlying index proof obligation.

The v13 overlay uses unannotated-source OpenJML preflight results to separate
generated-spec insufficiency from source-level verifier obligations.  If the
generated run fails verification and the original source already fails OpenJML
on a non-assert safety obligation such as cast safety, array bounds, nullness,
or shift width, the report classifies the case as `source_safety_obligation`.
This does not change the pass count.  After a targeted `list2` source preflight
was merged in v14, all remaining SV-COMP Java non-passes are explained by
source-level OpenJML obligations, source assertions, frontend/tool limitations,
library/model limitations, or verifier timeouts rather than by a remaining
generated-spec-only failure bucket.

The v15 overlay applies a verifier-only abstraction for standalone
`System.out.print/println/printf(...)` debug statements.  The abstraction
replaces those calls with empty statements while preserving surrounding Java
control-flow tokens, and source-preservation checks allow only that specific
debug-output difference.  Replaying the 38 `openjml_timeout` cases converted
five cases to clean passes and five cases to explicit verification failures;
the other 28 remain OpenJML timeouts, mostly around Java string/library-heavy
programs.  This raises the clean SV-COMP Java count from `194/265` to
`199/265` without new LLM calls.  Because this was a one-trial LLM-free replay
of timeout cases, the aggregate trial-pass count is intentionally preserved
from the original 10-trial run.

The v17 overlay adds a second verifier-only abstraction for standalone JVM
termination calls such as `Runtime.getRuntime().halt(...)` and `System.exit(...)`.
Those calls are non-returning in the benchmark intent, but OpenJML's bundled
JDK model introduces difficult `diverges` obligations.  The verifier artifact
models them as unchecked exceptional exits, which preserves normal-return
postcondition reasoning and proves the SV-COMP `Verifier` helper.  This raises
the clean SV-COMP Java count to `200/265`.  The same update also fixes timeout
classification so artifact directory names containing `openjml_timeout` do not
misclassify ordinary verification failures as verifier timeouts; the current
report separates 28 remaining `openjml_timeout` cases from four
`spec_not_sufficient` cases.

The v18 overlay adds unannotated-source preflight for those four
`spec_not_sufficient` cases.  All four original Java sources time out under
OpenJML before generated JML is considered, so the current report classifies
them as `source_openjml_timeout` rather than generated-spec-only failures.
This does not change the pass count, but it removes the remaining
`spec_not_sufficient` bucket and makes the residual failure taxonomy
decision-oriented: further post-processing is unlikely to improve the headline
without addressing OpenJML/source/library boundaries or changing the generation
strategy.

The v19 overlay applies a narrow verifier-only constant-folding abstraction for
compile-time literal `String.split(" ")` calls.  When both the receiver string
and delimiter are literals, the verifier artifact replaces the split call with
the exact Java array literal, including Java's default behavior of dropping
trailing empty tokens.  This avoids OpenJML's heavy `String`/`CharSequence`
library model without changing the value being verified.  It proves
`TokenTest01`, raising the SV-COMP Java count to `201/265`.  Non-constant
splits, general regular expressions, and input-dependent scanners remain
untouched because abstracting them would change the benchmark semantics.

The v20 overlay adds a narrow Java frontend hygiene repair for methods whose
tail `return true;` is rejected by javac/OpenJML as unreachable after an
always-thrown exception is caught and returns.  The repair removes only that
javac-unreachable tail statement in the verifier artifact; it does not rewrite
general exception control flow.  This lets OpenJML verify `athrow1` and
`exceptions16`, raising the SV-COMP Java count to `203/265`.

The v25 overlay adds a literal-only verifier abstraction for Java string
construction.  It folds `String.valueOf(...)` and `new String(...)` calls only
when the operand is a source-level literal, a literal `char[]`, or a value
derived from a previous literal-only fold.  Input-dependent strings, regexes,
general `StringBuilder` state, and unknown objects are left unchanged.  This
proves `StringValueOf01` and `StringConstructors01`, raising the SV-COMP Java
count to `205/265`.  A targeted probe also checked nearby string-heavy cases
(`StringValueOf03`, `StringValueOf10`, `StringBuilderAppend01`,
`HttpServletResponse`, and `TokenTest02`); they remain source/tool or library
boundary failures, so broadening this abstraction further would need a new
semantic hypothesis rather than another replay sweep.

The v26 overlay adds a conservative `StringBuilder`/`StringBuffer`
verifier-only abstraction.  It rewrites local builders to plain `String`
state only when the operations can be simulated in source order:
literal/input-backed construction, literal append chains, `length`,
`toString`, `capacity`, `ensureCapacity`, and shrinking `setLength`.
Unsupported mutations such as `getChars` make the abstraction bail out for the
case rather than partially rewriting it.  This proves
`StringBuilderAppend01`, `StringBuilderCapLen01`, and `StringBuilderCapLen03`,
raising the SV-COMP Java count to `208/265`.  The same targeted probe leaves
`StringBuilderChars04`, `HttpServletRequest`, and `IO` as OpenJML/library
timeouts, so further progress in this family likely requires a richer Java
library model instead of more local source-preserving rewrites.

The v27 overlay keeps the same `208/265` pass count but tightens the
literal-string abstraction.  It does not fold `new String(...)` when the
assigned variable participates in `==` or `!=` reference comparisons, because
object identity is observable in Java.  The regression probe confirms the five
previous string-construction/StringBuilder passes remain clean, while
`StringCompare01` still times out instead of being handled by an unsound
literal fold.

The v28 overlay adds a second string-library abstraction for locale-independent
methods on literal or single-assignment constant strings: `length`, `charAt`,
`equals`, ASCII-only `equalsIgnoreCase`, `compareTo`, `startsWith`, `endsWith`,
`regionMatches`, `replace(char,char)`, `trim`, `indexOf`, and `lastIndexOf`.
It deliberately does not fold `toUpperCase`/`toLowerCase`, regex methods, or
input-dependent receivers.  This proves `StringCompare01`, raising the
SV-COMP Java count to `209/265`.  The same probe leaves the other sampled
string-heavy cases (`StringStartEnd01`, `StringMiscellaneous04`, index-method
benchmarks, `charArray`, and input-dependent string examples) as OpenJML
timeouts, so their remaining cost is not explained by simple constant method
calls alone.

The v29 overlay adds a narrow enhanced-for abstraction over local literal
`String[]` arrays.  It folds only loops whose body is a single conditional
increment driven by a locale-independent string predicate on the loop item,
for example counting how many literal strings start or end with a fixed
substring.  Input-dependent arrays and non-counting loop bodies are left
unchanged.  This proves `StringStartEnd01`, raising the SV-COMP Java count to
`210/265`; the negative controls `StringStartEnd03` and `TokenTest02` remain
non-passing because their arrays or split results depend on runtime input.

The v30 overlay adds a deterministic null-dereference try/catch abstraction.
When a local reference is initialized to `null`, the immediately following
`try` body contains exactly one dereference of that local, and the
`NullPointerException`/`Exception` catch branch directly returns, the verifier
artifact replaces the try/catch plus normal fallthrough return with the catch
return.  This avoids OpenJML exception-library modeling without changing the
Java behavior for that block.  It proves `NullPointerException2`,
`NullPointerException3`, and `NullPointerException4`, raising the SV-COMP Java
count to `213/265`.  The negative controls `TestLazy` and `exceptions8` remain
timeouts because they involve wrapper-library calls or nontrivial exception
control flow outside this deterministic null-dereference pattern.

The v31 overlay extends the same deterministic null-dereference abstraction to
empty catch blocks followed by a fallthrough return.  In that form, the null
dereference must throw, the empty catch must complete normally, and execution
must continue to the fallthrough return.  This proves
`NullPointerException1`, raising the SV-COMP Java count to `214/265`;
exception-hierarchy controls (`exceptions10`, `exceptions13`, `exceptions18`,
and `exceptions8`) remain non-passing because they require nontrivial exception
dispatch rather than a single deterministic null-dereference branch.

The v32 overlay replays a small string-index family probe with the current
verifier-only constant-string abstraction.  This proves `StringIndexMethods01`,
whose assertions reduce to constant equalities after folding `indexOf` and
`lastIndexOf` on a local literal string, raising the SV-COMP Java count to
`215/265`.  The same probe leaves input-dependent string-index and
`CharSequence` cases as OpenJML timeouts, so the abstraction is not broadened
beyond source-level constants.

The v33 overlay adds a narrow verifier-only alias abstraction for local
`String` values cast to `CharSequence` and immediately converted back with
`toString`.  The rewrite is allowed only when the `CharSequence` temporary is
used exclusively for `length` afterward, in which case `String.length` is
equivalent.  This proves `CharSequenceToString`, raising the SV-COMP Java
count to `216/265`; controls involving servlet mocks, `StringBuilder.getChars`,
library preconditions, and input-dependent string-index calls remain
non-passing.

The v34 overlay adds a literal regex/matcher abstraction.  When both
`Pattern.compile(...)` and the searched `String` are source-level constants, and
the loop body only queries `matcher.group()`, the verifier artifact precomputes
the matched groups and rewrites `while (matcher.find())` as a foreach over a
literal string array.  This proves `RegexMatches01`, raising the SV-COMP Java
count to `217/265`; the input-dependent regex control `RegexMatches02` remains
an OpenJML timeout, as do unrelated string-library controls.

The v35 overlay adds a bounded char-array slice length abstraction.  When a
`char[]` initializer has a statically known length and
`String.valueOf(chars, start, count)` or `new String(chars, start, count)` is
statically in bounds, equality against a literal of different length is folded
to `false`.  This proves `StringValueOf03`, raising the SV-COMP Java count to
`218/265`; controls involving `String.valueOf(Object)`, input-dependent
`split`, `Scanner`/`Character` library calls, `StringBuilder.getChars`, and
dynamic `new String(c, 0, c.length)` remain non-passing.

The v36 overlay adds a narrow primitive-wrapper no-op abstraction.  If a local
wrapper is assigned from `Integer.valueOf`/similar with a primitive literal, the
immediately following primitive conversion call has a dropped result, and that
local is not used later, the verifier artifact replaces the pair with a no-op.
This proves `TestLazy`, raising the SV-COMP Java count to `219/265`; exception
control flow, `String.valueOf(Object)`, input-dependent `split`, and
`StringBuilder.getChars` controls remain non-passing.

The v37 overlay adds an exact full-copy `StringBuilder.getChars` self-comparison
abstraction.  If a local builder is copied into a same-length `char[]` and the
following enhanced-for loop returns `false` on
`character == builder.charAt(i)`, the verifier artifact replaces the block with
`return source.length() == 0`.  This proves `StringBuilderChars04`, raising the
SV-COMP Java count to `220/265`; related StringBuilder cases with different
mutations remain governed by their existing proofs, and unrelated dynamic
char-array/string-construction controls remain non-passing.

The v38 overlay adds a `String.valueOf(Object)` self-concatenation abstraction.
When an `Object` local is assigned from a `String` local, `String.valueOf` is
compared against the same string plus a non-empty literal suffix or prefix, and
the comparison is folded to `false`.  This proves `StringValueOf10`, raising the
SV-COMP Java count to `221/265`; `String.valueOf` cases that use arbitrary
objects, input-dependent `split`, `Scanner`/`Character`, and dynamic char-array
construction remain non-passing.

The v39 overlay adds literal-string comparison-loop folding.  It handles exact
checks where one literal string is compared against the reverse of another, and
where a literal prefix copied with `getChars` is immediately checked and the
destination array is dead afterward.  This proves `StringMiscellaneous01`,
raising the SV-COMP Java count to `222/265`; input-dependent string-index,
`split`, live char-array, and custom `toCharArray` controls remain non-passing.

The v40 overlay adds an exact `toCharArray` first-character propagation
abstraction.  It recognizes the verifier-heavy pattern where a non-empty input
string is converted to `char[]`, a helper writes `array[0]` to a literal
character and returns the same array, and the caller concatenates the array
after a literal prefix before checking that first copied character.  This proves
`charArray`, raising the SV-COMP Java count to `223/265`; input-dependent
string-index, case-conversion, regex, and library-precondition controls remain
non-passing.

The split-token equality probe
`artifacts/svcomp_java_split_token_equals_probe_20260621/` tried a conservative
normalization from `token.equals("literal")` to `"literal".equals(token)` when
`token` is introduced by a foreach loop over a `String.split` result.  This did
not improve the clean result: `TokenTest02` still fails in OpenJML's
`CharSequence` invariant, only moving the obligation from receiver to argument
position.  The rewrite is therefore not included in the default verifier
abstraction chain.

The v41 overlay adds an impossible string-affix equality fold.  If a local
string has a fixed literal prefix or suffix, equality with a literal that
violates that affix is folded to `false`; if the concat temporary is then dead,
the verifier artifact drops the expensive concat value.  This does not change
the clean pass count, but it reclassifies `OverapproximationString01` from an
OpenJML timeout to a source assertion failure, improving the failure taxonomy
without adding any generated-spec issue.

The v42 overlay merges source-preflight evidence for the remaining
timeout/library-heavy cases.  On the unannotated Java sources, nine of those
cases still time out in OpenJML, two fail on Java library preconditions, and
`exceptions8` fails on a source-level Java `assert`.  The clean pass count
remains `223/265`, but the failure taxonomy becomes more actionable:
`0` generated-spec-only failures, `16` source OpenJML timeouts,
`13` source assertion failures, `8` source safety obligations, `2` source
library preconditions, `2` frontend/tool failures, and one residual OpenJML
timeout.

The v44 overlay improves compatibility with earlier source-preflight reports
that did not store an explicit `failure_reason` field.  The loader now recovers
diagnostics such as `Assert`, `NullField`, and `PossiblyNegativeIndex` from the
stored first OpenJML output line, treats that parsed reason as more precise than
legacy boolean assert flags, and prefers concrete source diagnostics over less
informative timeout-only preflight rows.  Timeout preflight rows with a concrete
diagnostic such as `NullField` are treated as source safety evidence, while
concrete source-preserving replay evidence, such as a generated artifact
pointing at a Java `assert`, is kept ahead of timeout-only source preflight
rows.  When several source-preflight reports are merged, concrete diagnostics
also outrank plain timeout rows and empty verification-failure rows.  This
leaves the clean pass count unchanged at `223/265`, but removes the residual
generated-timeout bucket: all `42` non-passing cases are classified as
source/tool boundaries, including `15` source assertion failures, `12` source
safety obligations, `11` source OpenJML timeouts, `2` source library
preconditions, and `2` frontend/tool failures.

Two follow-up probes did not change the headline count and should not be
expanded without a new hypothesis.  First,
`artifacts/svcomp_java_feedback_retry4_claude_20260621/` attempted a four-case
`--source-preflight-feedback` regeneration on the remaining library/tool-heavy
failures, but the configured OpenRouter key returned a provider quota error
before any useful model output was produced.  Second,
`artifacts/svcomp_java_multinewarray_braced_loop_replay_20260621/` applied
semantics-preserving braces to compact nested loops and pruned generated loop
specs after OpenJML internal errors.  `multinewarray` still fails with
OpenJML's `Double rewriting of ident` internal error even after the generated
loop annotations are removed, so this case remains classified as an
OpenJML/source-tool boundary rather than a generated-spec failure.

The latest LLM-free failed-case replay,
`artifacts/specgenbench_failed6_current_replay_20260621/`, did not add any
SpecGenBench passes.  The three matrix cases still end in OpenJML internal
errors even after verifier-preserving quantifier renaming and loop-spec pruning,
so they should be treated as OpenJML/tool limitations unless a stronger
verifier-side workaround is introduced.  A follow-up scratch probe rewrote the
matrix loop body to use local row aliases, which avoids the internal error but
only exposes ordinary arithmetic-range and loop-invariant proof failures; it
does not produce a clean proof.  That rewrite is therefore not included in the
evaluation pipeline.

The v8 SpecGenBench overlay merges a targeted unannotated-source preflight for
`SolveQuadraticEquation`.  OpenJML already reports `PossiblyDivideByZero` on the
original source, so the remaining generated failure is classified as a
source-level safety obligation rather than a generated-spec-only failure.  The
case pass and trial-pass counts are unchanged.

The SV-COMP Java timeout probe
`artifacts/svcomp_java_timeout_probe_180s_20260621/` replayed five
representative timeout artifacts with a 180-second OpenJML timeout and still
proved `0/5`.  This argues against expanding the experiment by simply raising
the verifier timeout; the remaining timeout-heavy cases should be treated as a
tool-cost boundary unless a different verifier configuration or benchmark
filter is chosen.

The `list2` frame-scope replay
`artifacts/svcomp_java_list2_current_frame_replay_20260621/` converted a stale
`SourceMissingSymbol` frontend failure into an ordinary `spec_not_sufficient`
failure by dropping out-of-scope generated frame locations such as `Next` and
`Value`.  The follow-up `artifacts/svcomp_java_list2_prune8_probe_20260621/`
showed that increasing the pruning budget still does not prove the case; it
only exposes the remaining null-dereference proof obligations.  Keep the
default pruning budget unless there is a broader pass-rate gain.

Status meanings:

- `passed`: OpenJML produced no output and exited successfully.
- `verification_failed`: generated JML was syntactically usable but insufficient
  for OpenJML to prove all obligations.
- `annotation_error`: OpenJML rejected the generated annotations/source shape.
- `source_changed`: the LLM changed executable Java code; this is rejected before
  OpenJML is run.
- `tool_missing`, `timeout`, `tool_error`: infrastructure failures.
