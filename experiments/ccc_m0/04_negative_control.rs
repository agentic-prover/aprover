// Negative control: deliberately wrong property — proves Kani+AProver can
// distinguish real failures from passes. Should produce verified=False.

fn utf8_sequence_length(b: u8) -> usize {
    if b < 0xC0 { 1 }
    else if b < 0xE0 { 2 }
    else if b < 0xF0 { 3 }
    else { 4 }
}

#[kani::proof]
fn proof_wrong_always_one() {
    // FALSE for any b >= 0xC0 — Kani should find b = 0xC0 as a counterexample.
    let b: u8 = kani::any();
    assert!(utf8_sequence_length(b) == 1);
}
