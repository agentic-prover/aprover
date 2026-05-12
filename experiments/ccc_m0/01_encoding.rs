// Kani harnesses for CCC pure functions — M0 smoke test.
//
// Functions copied verbatim from anthropics/claudes-c-compiler:
//   - utf8_sequence_length   (src/common/encoding.rs:65)
//   - decode_pua_byte        (src/common/encoding.rs:80)
//
// Each #[kani::proof] entry verifies a property that should hold for all inputs.

const PUA_BASE: u32 = 0xE080;

// --- CCC verbatim ---------------------------------------------------------

fn utf8_sequence_length(b: u8) -> usize {
    if b < 0xC0 { 1 }
    else if b < 0xE0 { 2 }
    else if b < 0xF0 { 3 }
    else { 4 }
}

pub fn decode_pua_byte(input: &[u8], pos: usize) -> (u8, usize) {
    if pos + 2 < input.len() && input[pos] == 0xEE {
        let b1 = input[pos + 1];
        let b2 = input[pos + 2];
        if b1 == 0x82 && (0x80..=0xBF).contains(&b2) {
            let orig = b2;
            return (orig, 3);
        } else if b1 == 0x83 && (0x80..=0xBF).contains(&b2) {
            let orig = 0xC0 + (b2 - 0x80);
            return (orig, 3);
        }
    }
    (input[pos], 1)
}

// --- Kani proofs ----------------------------------------------------------

#[kani::proof]
fn proof_utf8_sequence_length_in_range() {
    let b: u8 = kani::any();
    let n = utf8_sequence_length(b);
    // Result must be one of 1, 2, 3, 4 — total function over u8.
    assert!(n == 1 || n == 2 || n == 3 || n == 4);
}

#[kani::proof]
fn proof_utf8_sequence_length_ascii_is_one() {
    let b: u8 = kani::any();
    kani::assume(b < 0x80);
    // ASCII bytes are always single-byte sequences.
    assert!(utf8_sequence_length(b) == 1);
}

#[kani::proof]
fn proof_decode_pua_non_ee_is_passthrough() {
    // 4-byte bounded input, position 0.
    let input: [u8; 4] = kani::any();
    kani::assume(input[0] != 0xEE);
    let (out, consumed) = decode_pua_byte(&input, 0);
    assert!(out == input[0]);
    assert!(consumed == 1);
}

#[kani::proof]
fn proof_decode_pua_consumed_is_one_or_three() {
    let input: [u8; 4] = kani::any();
    let pos: usize = kani::any();
    kani::assume(pos < input.len());
    let (_, consumed) = decode_pua_byte(&input, pos);
    assert!(consumed == 1 || consumed == 3);
}

#[kani::proof]
fn proof_decode_pua_roundtrip_high_bytes() {
    // Round-trip property: encoding then decoding any byte in 0x80..=0xFF
    // should yield the original byte.  This is the invariant CCC's own
    // test_roundtrip_all_bytes asserts dynamically — here we prove it
    // symbolically for the whole 0x80..=0xFF range.
    let b: u8 = kani::any();
    kani::assume(b >= 0x80);
    // Hand-encode b as a 3-byte PUA UTF-8 sequence per the encoding scheme
    // in bytes_to_string -> encode_non_utf8 (src/common/encoding.rs:57):
    //   char::from_u32(PUA_BASE + (b - 0x80) as u32).unwrap()
    // For b in 0x80..=0xBF: U+E080..U+E0BF -> EE 82 (b)
    // For b in 0xC0..=0xFF: U+E0C0..U+E0FF -> EE 83 (0x80 + (b - 0xC0))
    let encoded: [u8; 3] = if b <= 0xBF {
        [0xEE, 0x82, b]
    } else {
        [0xEE, 0x83, 0x80 + (b - 0xC0)]
    };
    // Wrap in a longer slice so the bounds check `pos + 2 < input.len()` passes.
    let buf: [u8; 4] = [encoded[0], encoded[1], encoded[2], 0];
    let (decoded, consumed) = decode_pua_byte(&buf, 0);
    assert!(consumed == 3);
    assert!(decoded == b);
}

// Silence unused-const warning under Kani (PUA_BASE is referenced only in docs).
#[allow(dead_code)]
fn _keep_pua_base() -> u32 { PUA_BASE }
