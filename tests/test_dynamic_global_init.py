"""Dynamic harness allocates init-trusted pointer globals (Bug B).

The CBMC harness assumes `g != NULL` for init-trusted globals (Step 1.5c). The
dynamic harness includes the source global (`mem_root = NULL`) but never runs the
init function, so a real callee that walks the global NULL-derefs and reports a
false `confirmed_dynamic`. The dynamic harness must allocate init-trusted pointer
globals to mirror the CBMC assumption.
"""

from types import SimpleNamespace

from bmc_agent.harness_generator import _emit_dynamic_global_invariant_inits


_SRC = """
static vfs_node_t *mem_root = ((void *)0);
static const char *banner = "v";
void vfs_init(void){ mem_root = alloc_inode(); }
int vfs_lookup(void){ return mem_root->size; }
"""


def _pf(src):
    return SimpleNamespace(preprocessed_source=src, path=None)


def _cfg(enabled=True):
    return SimpleNamespace(enable_global_invariants=enabled)


def test_init_trusted_pointer_is_allocated():
    out = _emit_dynamic_global_invariant_inits(_pf(_SRC), _cfg(), {"mem_root"})
    joined = "\n".join(out)
    assert "mem_root = calloc(1, sizeof(*mem_root))" in joined
    assert "if (!mem_root)" in joined  # guard: don't clobber a set value


def test_proven_const_table_not_allocated():
    # `banner` is a const table (proven, already non-NULL) -> no calloc.
    out = _emit_dynamic_global_invariant_inits(_pf(_SRC), _cfg(), {"mem_root", "banner"})
    assert not any("banner" in l for l in out)


def test_gated_by_referenced_names():
    # If mem_root isn't referenced by the closure, don't emit (would not compile).
    out = _emit_dynamic_global_invariant_inits(_pf(_SRC), _cfg(), {"something_else"})
    assert out == []


def test_disabled_flag_emits_nothing():
    out = _emit_dynamic_global_invariant_inits(_pf(_SRC), _cfg(enabled=False), {"mem_root"})
    assert out == []


def test_no_source_is_noop():
    out = _emit_dynamic_global_invariant_inits(SimpleNamespace(preprocessed_source=None, path=None),
                                               _cfg(), {"mem_root"})
    assert out == []
