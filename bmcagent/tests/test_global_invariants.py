"""Tests for the evidence-grounded global-invariant extractor.

The load-bearing assertions are the NEGATIVE ones: the extractor must REFUSE
to invariant-ize an attacker-tainted or generally-written global, because a
wrong project invariant silently suppresses real bugs everywhere.
"""

from bmc_agent.global_invariants import (
    extract_global_invariants,
    emit_assume_statements,
)


def _emitted(src, **kw):
    invs = extract_global_invariants(src, **kw)
    return {i.name: i for i in invs if i.emitted}


def _rejected(src, **kw):
    invs = extract_global_invariants(src, **kw)
    return {i.name: i for i in invs if not i.emitted}


# --------------------------------------------------------------------------
# TIER A — proven (const)
# --------------------------------------------------------------------------
def test_const_pointer_table_is_nonnull():
    src = 'static const char *const g_name = "vibeos";\n'
    e = _emitted(src)
    assert "g_name" in e
    assert e["g_name"].clause == "g_name != NULL"
    assert e["g_name"].tier == "proven"


def test_const_array_table_is_nonnull():
    src = "static const unsigned char crc_tab[256] = { 1, 2, 3 };\n"
    e = _emitted(src)
    assert e["crc_tab"].clause == "crc_tab != NULL"
    assert e["crc_tab"].tier == "proven"


def test_const_scalar_equals_literal():
    src = "static const int MAX_FONTS = 16;\n"
    e = _emitted(src)
    assert e["MAX_FONTS"].clause == "MAX_FONTS == 16"
    assert e["MAX_FONTS"].tier == "proven"


def test_const_scalar_hex_literal():
    src = "const uint32_t MAGIC = 0x7F454C46;\n"
    e = _emitted(src)
    assert e["MAGIC"].clause == "MAGIC == 0x7F454C46"


def test_const_pointer_null_init_is_rejected():
    src = "static const char *empty = NULL;\n"
    r = _rejected(src)
    assert "empty" in r and "non-NULL" in r["empty"].evidence


# --------------------------------------------------------------------------
# TIER B — init-trusted (the taint gate is the point)
# --------------------------------------------------------------------------
def test_pointer_set_only_in_init_is_emitted():
    src = """
static vfs_node_t *vfs_root = 0;
void vfs_init(void) {
    vfs_root = kmalloc(sizeof(vfs_node_t));
}
int vfs_read(void *buf, int n) {
    return vfs_root->size;
}
"""
    e = _emitted(src)
    assert "vfs_root" in e
    assert e["vfs_root"].clause == "vfs_root != NULL"
    assert e["vfs_root"].tier == "init-trusted"


def test_pointer_written_in_normal_fn_is_rejected():
    # vfs_root reassigned in a non-init function -> NOT an invariant.
    src = """
static vfs_node_t *vfs_root = 0;
void vfs_init(void) { vfs_root = kmalloc(8); }
void vfs_chroot(vfs_node_t *n) { vfs_root = n; }
"""
    r = _rejected(src)
    assert "vfs_root" in r
    assert "outside an init" in r["vfs_root"].evidence


def test_init_write_from_parameter_is_tainted_and_rejected():
    # The init write derives from a function PARAMETER -> attacker-influenced.
    src = """
static dev_t *cur_dev = 0;
void dev_init(dev_t *attacker_supplied) {
    cur_dev = attacker_supplied;
}
"""
    r = _rejected(
        src,
        fn_param_names={"dev_init": {"attacker_supplied"}},
    )
    assert "cur_dev" in r
    assert "taint" in r["cur_dev"].evidence.lower()


def test_address_taken_global_is_rejected():
    # &g hands out a write path we can't track -> reject.
    src = """
static node_t *root = 0;
void boot_init(void) { root = make(); }
void wire(void) { node_t **slot = &root; *slot = 0; }
"""
    r = _rejected(src)
    assert "root" in r
    assert "address taken" in r["root"].evidence


def test_init_trusted_disabled_flag():
    src = """
static node_t *root = 0;
void sys_init(void) { root = make(); }
"""
    r = _rejected(src, emit_init_trusted=False)
    assert "root" in r and "disabled" in r["root"].evidence


# --------------------------------------------------------------------------
# General rejections / robustness
# --------------------------------------------------------------------------
def test_extern_is_rejected():
    src = "extern char *xmlMalloc;\n"
    r = _rejected(src)
    assert "xmlMalloc" in r and "extern" in r["xmlMalloc"].evidence


def test_plain_mutable_scalar_not_emitted():
    src = "int g_counter = 0;\nvoid bump(void){ g_counter++; }\n"
    assert "g_counter" not in _emitted(src)


def test_referenced_names_filter():
    src = """
static const char *a = "x";
static const char *b = "y";
"""
    e = _emitted(src, referenced_names={"a"})
    assert "a" in e and "b" not in e


def test_comment_and_string_noise_ignored():
    # A write inside a comment / string must not count as a real write.
    src = """
static const char *banner = "root = 0; vfs_root = NULL;";
/* vfs_root = NULL; */
static node_t *vfs_root = 0;
void kboot_init(void){ vfs_root = make(); }
"""
    e = _emitted(src)
    assert e["banner"].clause == "banner != NULL"
    assert e["vfs_root"].clause == "vfs_root != NULL"


def test_emit_assume_statements():
    src = 'static const char *n = "v";\n'
    invs = extract_global_invariants(src)
    stmts = emit_assume_statements(invs)
    assert stmts == ["__CPROVER_assume(n != NULL);"]


def test_no_globals_returns_empty():
    src = "int f(int x){ return x+1; }\n"
    assert emit_assume_statements(extract_global_invariants(src)) == []


def test_preprocessed_null_initializer_with_parens():
    # After cpp, NULL -> ((void *)0): the '(' is in the INITIALIZER, not the
    # declarator. The decl must still parse (regression: a naive '(' check
    # rejected the whole statement and dropped every preprocessed global).
    src = """
static vfs_node_t *mem_root = ((void *)0);
static node_t *root = ((void *)0);
void vfs_init(void) { mem_root = alloc_inode(); }
int vfs_lookup(void){ return mem_root->size; }
"""
    e = _emitted(src)
    assert "mem_root" in e
    assert e["mem_root"].clause == "mem_root != NULL"
    assert e["mem_root"].tier == "init-trusted"
    # `root` is never written in an init fn -> stays NULL -> NOT emitted.
    assert "root" not in e


def test_func_pointer_variable_in_declarator_is_rejected():
    # A '(' in the DECLARATOR (function-pointer variable) must still be skipped.
    src = "static int (*handler)(int) = some_fn;\n"
    assert "handler" not in _emitted(src)
