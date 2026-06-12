"""(buf, len) harness pairing: a byte buffer is sized to its length param.

General C convention `f(const uint8_t *buf, <int> len)` where buf points to len
bytes. Without pairing the harness gives buf a fixed small buffer while len is
unconstrained-large -> spurious OOB on every length-driven read (vibeos net
icmp/ip/tcp_handle). Fix: buf = malloc(len) (exact size: reads in-bounds, and an
off-by-one PAST len is still caught — not masked by an over-sized buffer).
"""

from types import SimpleNamespace

from bmc_agent.harness_generator import _detect_buf_len_pairs, _generate_nd_decls


def test_pairs_byte_pointer_with_adjacent_size_param():
    p = [("const uint8_t *", "pkt"), ("uint32_t", "len"), ("uint32_t", "src_ip")]
    assert _detect_buf_len_pairs(p) == [("pkt", "const uint8_t *", "len", "uint32_t")]


def test_pairs_various_size_names_and_types():
    assert _detect_buf_len_pairs([("const unsigned char *", "data"), ("size_t", "n")])
    assert _detect_buf_len_pairs([("uint8_t *", "b"), ("int", "length")])


def test_plain_char_pointer_not_paired_string_convention():
    # `char *` is the NUL-terminated-string convention, not an n-byte buffer.
    assert _detect_buf_len_pairs([("const char *", "data"), ("size_t", "n")]) == []
    assert _detect_buf_len_pairs([("char *", "s"), ("int", "len")]) == []


def test_struct_pointer_not_paired():
    # element/struct arrays are left to infer_array_param_bounds, not byte-paired.
    assert _detect_buf_len_pairs([("struct pkt *", "p"), ("int", "len")]) == []


def test_void_pointer_not_paired():
    # void* has no element size; handled by its own branch.
    assert _detect_buf_len_pairs([("const void *", "data"), ("size_t", "size")]) == []


def test_non_size_named_sibling_not_paired():
    # f(const char *path, int flags) -- path is a string, flags isn't a length.
    assert _detect_buf_len_pairs([("const char *", "path"), ("int", "flags")]) == []


def test_double_pointer_not_paired():
    assert _detect_buf_len_pairs([("const uint8_t **", "pp"), ("size_t", "n")]) == []


def test_harness_emits_malloc_sized_to_len():
    sig = SimpleNamespace(parameters=[("const uint8_t *", "pkt"),
                                      ("uint32_t", "len"), ("uint32_t", "src_ip")])
    func = SimpleNamespace(name="h", signature=sig, body="if(len<8)return; x=pkt[0];")
    out = "\n".join(_generate_nd_decls(func, cbmc_unwind=4))
    assert "pkt = (const uint8_t *)malloc(len)" in out   # sized to len
    assert "_pkt_buf" not in out                          # NOT a fixed buffer
    assert "(unsigned long long)(len) <=" in out          # len bounded (any int type)
    assert "__CPROVER_assume(pkt != NULL)" in out


def test_unpaired_byte_pointer_still_gets_fixed_buffer():
    # A lone byte pointer (no length sibling) keeps the fixed-buffer treatment.
    sig = SimpleNamespace(parameters=[("const char *", "name")])
    func = SimpleNamespace(name="g", signature=sig, body="return name[0];")
    out = "\n".join(_generate_nd_decls(func, cbmc_unwind=4))
    assert "_name_buf" in out and "malloc(name)" not in out
