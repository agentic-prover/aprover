// Kani harnesses for CCC peephole helper — M0 smoke test.
//
// Function copied verbatim from anthropics/claudes-c-compiler:
//   - is_ident_char  (src/backend/peephole_common.rs:18)

#[inline]
fn is_ident_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'.' || b == b'_'
}

// --- Kani proofs ----------------------------------------------------------

#[kani::proof]
fn proof_is_ident_char_classes() {
    let b: u8 = kani::any();
    let r = is_ident_char(b);
    // Exhaustive characterisation: r iff b is in one of the documented classes.
    let in_digit  = b >= b'0' && b <= b'9';
    let in_lower  = b >= b'a' && b <= b'z';
    let in_upper  = b >= b'A' && b <= b'Z';
    let in_punct  = b == b'.' || b == b'_';
    assert!(r == (in_digit || in_lower || in_upper || in_punct));
}

#[kani::proof]
fn proof_is_ident_char_no_whitespace() {
    // Whitespace bytes must NOT be identifier characters — peephole word-boundary
    // logic depends on this.
    let b: u8 = kani::any();
    kani::assume(b == b' ' || b == b'\t' || b == b'\n' || b == b'\r');
    assert!(!is_ident_char(b));
}

#[kani::proof]
fn proof_is_ident_char_no_comma() {
    // Comma is the assembly operand separator; must not be considered part of
    // a register name token.
    assert!(!is_ident_char(b','));
}
