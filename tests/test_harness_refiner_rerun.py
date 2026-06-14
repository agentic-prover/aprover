"""End-to-end (synthetic, no LLM) test of DynamicValidator.refine_and_revalidate.

Proves the empirical Phase-1 decision mechanism with a real GCC compile+run:
  - a harness derefs an UNDEFINED extern pointer (links to 0 -> SIGSEGV);
  - a sibling .c defines it as boot-init-trusted (NULL init, set only in *_init);
  - after materialization the deref hits a calloc'd object -> NOT_TRIGGERED.
And the dual: a harness that walks PAST index 0 still faults on the 1-element
materialized buffer -> CONFIRMED (a real OOB is never masked).
"""

import shutil
from types import SimpleNamespace

import pytest

from bmc_agent.dynamic_validator import DynamicValidator, DynamicOutcome

pytestmark = pytest.mark.skipif(
    shutil.which("gcc") is None, reason="gcc not available for dynamic re-run test"
)


def _validator():
    cfg = SimpleNamespace(
        dynamic_cc_path="gcc",
        include_dirs=[],
        cbmc_defines=[],
        dynamic_validation_timeout=10,
    )
    return DynamicValidator(cfg, harness_gen=None, llm=None)


# Harness: derefs extern int *g_trusted (undefined -> 0 without materialization).
_HARNESS_NULL_DEREF = r"""
#include <stdio.h>
#include <signal.h>
#include <unistd.h>
extern int *g_trusted;
static volatile int fut_called = 0;
static void onseg(int s){ (void)s; printf("DYNAMIC:CONFIRMED signal=SIGSEGV fut_called=%d\n", fut_called); fflush(stdout); _exit(0); }
static int target(void){ return g_trusted[0]; }
int main(void){
    signal(SIGSEGV, onseg);
    fut_called = 1;
    int v = target();
    printf("DYNAMIC:NOT_TRIGGERED v=%d\n", v);
    return 0;
}
"""

# Harness: walks to index 100 — a REAL OOB on a 1-element materialized buffer.
_HARNESS_REAL_OOB = r"""
#include <stdio.h>
#include <signal.h>
#include <unistd.h>
extern int *g_trusted;
static volatile int fut_called = 0;
static void onseg(int s){ (void)s; printf("DYNAMIC:CONFIRMED signal=SIGSEGV fut_called=%d\n", fut_called); fflush(stdout); _exit(0); }
static int target(void){ int acc=0; for (int i=0;i<100000;i++) acc += g_trusted[i]; return acc; }
int main(void){
    signal(SIGSEGV, onseg);
    fut_called = 1;
    int v = target();
    printf("DYNAMIC:NOT_TRIGGERED v=%d\n", v);
    return 0;
}
"""

_SIBLING = """
int *g_trusted = NULL;
void mod_init(void){ g_trusted = malloc(sizeof(int)); }
"""


def test_null_deref_artifact_is_cleaned_after_materialization():
    dv = _validator()
    out = dv.refine_and_revalidate(_HARNESS_NULL_DEREF, {"mod.c": _SIBLING})
    assert out is not None, "refinement should apply (undefined trusted extern)"
    res, plan = out
    assert [g.name for g in plan] == ["g_trusted"]
    # materialized pointer -> deref of index 0 is valid -> no fault
    assert res.outcome == DynamicOutcome.NOT_TRIGGERED


def test_real_oob_still_crashes_on_one_element_buffer():
    dv = _validator()
    out = dv.refine_and_revalidate(_HARNESS_REAL_OOB, {"mod.c": _SIBLING})
    assert out is not None
    res, _plan = out
    # walking far past the single calloc'd element still faults -> KEPT
    assert res.outcome == DynamicOutcome.CONFIRMED
    assert res.harness_kind == "unit_refined"


def test_no_trusted_extern_returns_none():
    dv = _validator()
    # harness that links cleanly (no undefined externs) -> not applicable
    clean = "int main(void){ return 0; }\n"
    assert dv.refine_and_revalidate(clean, {"mod.c": _SIBLING}) is None


# --- NULL-DEFINED (link-succeeds) artifact class ---------------------------
# The unit harness DEFINES the trusted global itself as NULL (pulled from the
# file under test), so it LINKS CLEAN — the old undefined-reference detection
# never fired. The fault is a runtime NULL-deref, not a link error.

_HARNESS_NULL_DEFINED_DEREF = r"""
#include <stdio.h>
#include <signal.h>
#include <unistd.h>
int *g_trusted = NULL;
static volatile int fut_called = 0;
static void onseg(int s){ (void)s; printf("DYNAMIC:CONFIRMED signal=SIGSEGV fut_called=%d\n", fut_called); fflush(stdout); _exit(0); }
static int target(void){ return g_trusted[0]; }
int main(void){
    signal(SIGSEGV, onseg);
    fut_called = 1;
    int v = target();
    printf("DYNAMIC:NOT_TRIGGERED v=%d\n", v);
    return 0;
}
"""

_HARNESS_NULL_DEFINED_REAL_OOB = r"""
#include <stdio.h>
#include <signal.h>
#include <unistd.h>
int *g_trusted = NULL;
static volatile int fut_called = 0;
static void onseg(int s){ (void)s; printf("DYNAMIC:CONFIRMED signal=SIGSEGV fut_called=%d\n", fut_called); fflush(stdout); _exit(0); }
static int target(void){ int acc=0; for (int i=0;i<100000;i++) acc += g_trusted[i]; return acc; }
int main(void){
    signal(SIGSEGV, onseg);
    fut_called = 1;
    int v = target();
    printf("DYNAMIC:NOT_TRIGGERED v=%d\n", v);
    return 0;
}
"""

# A sibling defining g_trusted as boot-init-trusted (set only in *_init).
_SIBLING_NULL_DEF = """
int *g_trusted = NULL;
void mod_init(void){ g_trusted = malloc(sizeof(int)); }
"""


def test_null_defined_artifact_is_cleaned_after_materialization():
    dv = _validator()
    out = dv.refine_and_revalidate(_HARNESS_NULL_DEFINED_DEREF, {"mod.c": _SIBLING_NULL_DEF})
    assert out is not None, "refinement should apply (NULL-defined trusted global, link-clean)"
    res, plan = out
    assert [g.name for g in plan] == ["g_trusted"]
    assert all(g.already_defined for g in plan)
    # constructor reassigns the already-defined NULL global -> deref of 0 is valid
    assert res.outcome == DynamicOutcome.NOT_TRIGGERED
    assert res.harness_kind == "unit_refined"


def test_null_defined_real_oob_still_crashes():
    dv = _validator()
    out = dv.refine_and_revalidate(_HARNESS_NULL_DEFINED_REAL_OOB, {"mod.c": _SIBLING_NULL_DEF})
    assert out is not None
    res, _plan = out
    # a genuine OOB walks past the single materialized element -> KEPT
    assert res.outcome == DynamicOutcome.CONFIRMED


def test_null_defined_but_already_materialized_returns_none():
    dv = _validator()
    # harness already materializes g_trusted -> nothing to refine, keep finding
    already = _HARNESS_NULL_DEFINED_DEREF.replace(
        "fut_called = 1;",
        "if(!g_trusted){ g_trusted = calloc(1,sizeof(int)); } fut_called = 1;",
    )
    assert dv.refine_and_revalidate(already, {"mod.c": _SIBLING_NULL_DEF}) is None
