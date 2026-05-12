// Kani harnesses for CCC pure arithmetic helpers — M0 smoke test.
//
// Functions copied verbatim from anthropics/claudes-c-compiler:
//   - wrap_result    (src/common/const_arith.rs:19)
//   - unsigned_op    (src/common/const_arith.rs:27)
//   - bool_to_i64    (src/common/const_arith.rs:37)

#[inline]
fn wrap_result(v: i64, is_32bit: bool) -> i64 {
    if is_32bit { v as i32 as i64 } else { v }
}

#[inline]
fn unsigned_op(l: i64, r: i64, is_32bit: bool, op: fn(u64, u64) -> u64) -> i64 {
    if is_32bit {
        op(l as u32 as u64, r as u32 as u64) as u32 as i64
    } else {
        op(l as u64, r as u64) as i64
    }
}

#[inline]
fn bool_to_i64(b: bool) -> i64 {
    if b { 1 } else { 0 }
}

// --- Kani proofs ----------------------------------------------------------

#[kani::proof]
fn proof_bool_to_i64_in_zero_one() {
    let b: bool = kani::any();
    let v = bool_to_i64(b);
    assert!(v == 0 || v == 1);
    assert!(b == (v == 1));
}

#[kani::proof]
fn proof_wrap_result_64bit_identity() {
    let v: i64 = kani::any();
    assert!(wrap_result(v, false) == v);
}

#[kani::proof]
fn proof_wrap_result_32bit_fits() {
    // When is_32bit, the result is the value re-cast as i32:i64, so it must
    // be in the i32 range and re-applying wrap is idempotent.
    let v: i64 = kani::any();
    let w = wrap_result(v, true);
    assert!(w >= i32::MIN as i64);
    assert!(w <= i32::MAX as i64);
    assert!(wrap_result(w, true) == w);
}

#[kani::proof]
fn proof_wrap_result_32bit_low_bits_preserved() {
    // The low 32 bits of v and wrap_result(v, true) must agree.
    let v: i64 = kani::any();
    let w = wrap_result(v, true);
    assert!((v as u32) == (w as u32));
}

#[kani::proof]
fn proof_unsigned_op_add_64bit() {
    let l: i64 = kani::any();
    let r: i64 = kani::any();
    let got = unsigned_op(l, r, false, u64::wrapping_add);
    let want = (l as u64).wrapping_add(r as u64) as i64;
    assert!(got == want);
}

#[kani::proof]
fn proof_unsigned_op_add_32bit_low_bits() {
    let l: i64 = kani::any();
    let r: i64 = kani::any();
    let got = unsigned_op(l, r, true, u64::wrapping_add);
    let want = (l as u32).wrapping_add(r as u32) as i64;
    assert!(got == want);
    // 32-bit result must be in u32 range when reinterpreted unsigned.
    assert!(got >= 0 && got <= u32::MAX as i64);
}
