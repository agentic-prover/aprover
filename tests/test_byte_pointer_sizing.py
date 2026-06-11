"""Byte-typedef pointer params get a sized backing BUFFER, not a 1-byte scalar.

Root cause (vibeos ttf calibration): the harness byte-pointer branch keyed only
on literal type names ("char"/"unsigned char"/"uint8_t"/...), so a `T *p` whose
`T` is a byte TYPEDEF (stbtt_uint8 = unsigned char) fell through to the generic
single-SCALAR default. An accessor like `ttUSHORT(stbtt_uint8 *p)` reading
p[0..1] then got a 1-byte object — so "verified clean" was meaningless (the
harness couldn't pose the OOB question soundly). Resolving the typedef routes it
to a real `_p_buf[cbmc_unwind+1]` buffer that enforces the size contract.
"""

from types import SimpleNamespace

from bmc_agent.harness_generator import _is_byte_shaped_type, _generate_nd_decls


def test_resolver_literal_byte_types():
    for t in ("char", "unsigned char", "signed char", "uint8_t", "int8_t"):
        assert _is_byte_shaped_type(t), t


def test_resolver_byte_typedefs():
    for t in ("stbtt_uint8", "png_byte", "u8", "s8", "my_uint8_t", "uchar"):
        assert _is_byte_shaped_type(t), t


def test_resolver_rejects_multibyte_and_nonbyte():
    for t in ("stbtt_uint16", "uint16_t", "uint32_t", "int", "long",
              "void", "foo_t", "size_t"):
        assert not _is_byte_shaped_type(t), t


def test_resolver_strips_const():
    assert _is_byte_shaped_type("const stbtt_uint8")
    assert _is_byte_shaped_type("const unsigned char")


def _alloc_lines(ptype, pname="p", body="return p[0]*256+p[1];"):
    sig = SimpleNamespace(parameters=[(ptype, pname)])
    func = SimpleNamespace(name="acc", signature=sig, body=body)
    return _generate_nd_decls(func, cbmc_unwind=4)


def test_byte_typedef_pointer_gets_buffer_not_scalar():
    out = _alloc_lines("stbtt_uint8 *")
    joined = "\n".join(out)
    assert "_p_buf[5]" in joined, joined          # cbmc_unwind+1 sized buffer
    assert "_p_val" not in joined                 # NOT a single scalar
    assert "(stbtt_uint8 *)_p_buf" in joined      # p points at the buffer


def test_plain_uint8_pointer_still_buffer():
    # Regression: the literal types must keep working.
    out = _alloc_lines("uint8_t *")
    assert any("_p_buf[5]" in l for l in out)


def test_multibyte_typedef_pointer_not_treated_as_byte_buffer():
    # stbtt_uint16* must NOT get the raw byte-buffer treatment (it's 2-byte).
    out = _alloc_lines("stbtt_uint16 *", body="return p[0];")
    assert not any("unsigned char _p_buf" in l for l in out)
