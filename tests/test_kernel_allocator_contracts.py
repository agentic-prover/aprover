"""Tests that the kernel allocator family gets allocator-shaped stub
return contracts (instead of nondet pointers).

Without these contracts, kmalloc'd buffers in kernel harnesses are
unconstrained nondet pointers, which produces spurious caller-contract
R_OK assertions on legitimate caller code (see
findings/empirical_validity_protocol_2026-05-22.md).
"""

from __future__ import annotations

from bmc_agent.harness_generator import _builtin_stub_return_contract


def _malloc_params() -> list[tuple[str, str]]:
    """Minimal (size, flags) param shape used by Linux kmalloc."""
    return [("unsigned long", "size"), ("unsigned int", "flags")]


def _calloc_params() -> list[tuple[str, str]]:
    """(n, size, flags) shape used by kcalloc / kmalloc_array."""
    return [
        ("unsigned long", "n"),
        ("unsigned long", "size"),
        ("unsigned int", "flags"),
    ]


def test_kmalloc_noprof_treated_as_malloc():
    out = _builtin_stub_return_contract("kmalloc_noprof", "void *", _malloc_params())
    assert out
    assert "__CPROVER_w_ok(result, size)" in out[0]


def test_kzalloc_noprof_treated_as_malloc():
    out = _builtin_stub_return_contract("kzalloc_noprof", "void *", _malloc_params())
    assert out
    assert "__CPROVER_w_ok(result, size)" in out[0]


def test_vmalloc_noprof_treated_as_malloc():
    out = _builtin_stub_return_contract("vmalloc_noprof", "void *", _malloc_params())
    assert out
    assert "__CPROVER_w_ok(result, size)" in out[0]


def test_kcalloc_noprof_treated_as_calloc():
    out = _builtin_stub_return_contract("kcalloc_noprof", "void *", _calloc_params())
    assert out
    # calloc-shape contract multiplies n * size.
    assert "n" in out[0] and "size" in out[0]
    assert "*" in out[0]  # the multiplication


def test_kmalloc_array_noprof_treated_as_calloc():
    out = _builtin_stub_return_contract(
        "kmalloc_array_noprof", "void *", _calloc_params()
    )
    assert out
    assert "n" in out[0] and "size" in out[0]


def test_krealloc_treated_as_realloc():
    # realloc shape: (ptr, size, flags)
    params = [
        ("const void *", "p"),
        ("unsigned long", "size"),
        ("unsigned int", "flags"),
    ]
    out = _builtin_stub_return_contract("krealloc_noprof", "void *", params)
    assert out
    assert "__CPROVER_w_ok(result, size)" in out[0]


def test_devm_kzalloc_treated_as_malloc():
    """devm_-prefixed Linux allocators have an extra ``dev`` arg up
    front; pattern detector should still find the size param."""
    params = [
        ("struct device *", "dev"),
        ("unsigned long", "size"),
        ("unsigned int", "flags"),
    ]
    out = _builtin_stub_return_contract("devm_kzalloc", "void *", params)
    assert out
    assert "__CPROVER_w_ok(result, size)" in out[0]


def test_unknown_kernel_helper_not_misclassified():
    """A name that LOOKS allocator-like but isn't (e.g.
    ``kfree_call_rcu``) must NOT get a malloc contract."""
    out = _builtin_stub_return_contract(
        "kfree_call_rcu", "void *", _malloc_params()
    )
    assert out == []
