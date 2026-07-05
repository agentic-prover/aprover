"""Standalone (whole-program) verification.

The compositional pipeline (``AMCPipeline``) verifies each FUNCTION in isolation
against a synthesised harness that makes its parameters nondeterministic /
attacker-controlled — answering "is this function safe for ANY caller?". That is
the right question for finding latent bugs in reusable functions, but it is NOT
what you want when you have a self-contained program with its own ``main`` and
concrete inputs.

Standalone mode answers the other question: "is THIS program, as written, safe?"
It runs CBMC over the whole translation unit from the program's real entry point
(``main`` by default) with NO harness synthesis and NO nondet injection — the
program supplies its own inputs. Loops with concrete bounds unwind fully, so with
a large-enough ``--unwind`` and ``--unwinding-assertions`` the result is a
complete proof for the program as written.

It also makes ACSL-style ``//@ assert E;`` annotations checkable by rewriting
them to ``__CPROVER_assert`` (CBMC otherwise treats ``//@`` as a comment).
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from bmc_agent.cbmc import CBMCResult, run_cbmc
from bmc_agent.logger import get_logger

logger = get_logger("standalone")

# //@ assert <expr> ;   (ACSL) -> __CPROVER_assert(<expr>, "acsl: <expr>");
# CBMC ignores //@ comments, so without this the functional assertions in the
# program would never be checked. Keep it simple: single-line assert clauses.
_ACSL_ASSERT = re.compile(r"//@\s*assert\b\s*(.+?)\s*;", re.IGNORECASE)


def translate_acsl_asserts(text: str) -> tuple[str, int]:
    """Rewrite ``//@ assert E;`` to ``__CPROVER_assert(E, "acsl: E");``.

    Returns (new_text, count). ``__CPROVER_assert`` is a CBMC intrinsic — no
    include needed. The original ``//@`` line is replaced in place so line
    numbers in CBMC output still line up with the source.
    """
    count = 0

    def _repl(m: "re.Match") -> str:
        nonlocal count
        count += 1
        expr = m.group(1).strip()
        esc = expr.replace("\\", "\\\\").replace('"', '\\"')
        return f'__CPROVER_assert({expr}, "acsl: {esc}");'

    return _ACSL_ASSERT.sub(_repl, text), count


def verify_standalone(
    source_file: str | Path,
    config,
    entry: str = "main",
    unwind: int = 64,
    timeout: int = 120,
) -> tuple[CBMCResult, int]:
    """Verify *source_file* as a standalone whole program from *entry*.

    Returns (CBMCResult, n_acsl_asserts_translated). Enables the full
    memory-safety check set plus signed-overflow and ``--unwinding-assertions``
    (always on in ``run_cbmc``) so an under-sized unwind is reported rather than
    silently assumed away.
    """
    source_file = Path(source_file)
    raw = source_file.read_text(encoding="utf-8", errors="replace")

    # 1. Make ACSL asserts checkable (BEFORE preprocessing, which strips comments).
    translated, n_acsl = translate_acsl_asserts(raw)
    if n_acsl:
        logger.info("standalone: translated %d //@ assert -> __CPROVER_assert", n_acsl)

    include_dirs = list(getattr(config, "include_dirs", None) or [])
    defines = list(getattr(config, "cbmc_defines", None) or [])

    # 2. Optionally preprocess (cc -E) when include dirs are in play; otherwise
    #    hand the (translated) source straight to CBMC, which has its own frontend.
    src_to_check = translated
    if getattr(config, "preprocess", False) and include_dirs:
        # Write the translated source to a temp .c, then preprocess that file so
        # the //@->__CPROVER_assert rewrite survives into the expanded TU.
        with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as tf:
            tf.write(translated)
            tmp_src = tf.name
        try:
            from bmc_agent.preprocessor import preprocess as _pp
            src_to_check = _pp(tmp_src, include_dirs=include_dirs, defines=defines,
                               cc=getattr(config, "dynamic_cc_path", "cc"))
        except Exception as exc:
            logger.warning("standalone: preprocessing failed (%r); using raw source", exc)
            src_to_check = translated

    with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as tf:
        tf.write(src_to_check)
        check_path = tf.name

    logger.info("standalone: CBMC over whole program, entry='%s', unwind=%d", entry, unwind)
    result = run_cbmc(
        harness_path=check_path,
        function=entry,
        unwind=unwind,
        timeout=timeout,
        cbmc_path=getattr(config, "cbmc_path", "cbmc"),
        # When we preprocessed ourselves, CBMC needs no -I; when we didn't,
        # pass them through so CBMC's own frontend can resolve #includes.
        include_dirs=([] if (getattr(config, "preprocess", False) and include_dirs)
                      else include_dirs),
        defines=([] if (getattr(config, "preprocess", False) and include_dirs)
                 else defines),
        # Full memory-safety + integer-overflow check set for a whole-program pass.
        bounds_check=True,
        pointer_check=True,
        signed_overflow_check=True,
        pointer_overflow_check=True,
        div_by_zero_check=True,
        conversion_check=False,
    )
    return result, n_acsl
