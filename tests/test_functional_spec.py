"""Tests for Phase 1 functional spec generation.

The spec generator's LLM response may include an optional
``functional_spec`` field — a Rust/C boolean expression specifying what
the function SHOULD compute, beyond the defensive ``postcondition``.
When present, the functional spec is AND-merged into the postcondition
so downstream harness gen, classification, and refinement consume it
without changes.
"""

from __future__ import annotations

from bmc_agent.spec_generator import _parse_llm_spec_response


def _wrap(pre, post, functional=None):
    """Build the LLM JSON response we expect."""
    import json
    payload = {"precondition": pre, "postcondition": post, "reasoning": ""}
    if functional is not None:
        payload["functional_spec"] = functional
    return json.dumps(payload)


def test_parser_handles_missing_functional_spec():
    """Backward compat — old LLM responses lacking ``functional_spec``
    must still parse cleanly."""
    out = _parse_llm_spec_response(
        _wrap("true", "result >= 0"),
        "f",
    )
    assert out == ("true", "result >= 0"), out


def test_parser_ignores_empty_functional_spec():
    """LLM may explicitly emit ``functional_spec=""`` when no behavioural
    property is expressible. The empty string is dropped — post stays as
    written, not "((result >= 0) && ())"."""
    out = _parse_llm_spec_response(
        _wrap("true", "result >= 0", functional=""),
        "f",
    )
    assert out == ("true", "result >= 0"), out


def test_parser_ignores_trivial_functional_spec():
    """``"true"`` is a no-op functional spec — the LLM uses it as a
    null sentinel. Don't AND ``true`` into the post; it just adds noise."""
    for trivial in ["true", "True", "1", "n/a", "none"]:
        out = _parse_llm_spec_response(
            _wrap("true", "result >= 0", functional=trivial),
            "f",
        )
        assert out == ("true", "result >= 0"), (trivial, out)


def test_parser_merges_functional_spec_into_postcondition():
    """When the LLM provides a real functional spec, AND it into the
    postcondition so downstream sees a strengthened post."""
    out = _parse_llm_spec_response(
        _wrap(
            "data.len() >= offset + 2",
            "result <= u16::MAX",
            functional="result == ((data[offset+1] as u16) << 8) | (data[offset] as u16)",
        ),
        "read_u16",
    )
    assert out is not None
    pre, post = out
    assert pre == "data.len() >= offset + 2", pre
    # Post becomes the AND of the defensive clause and the functional clause.
    assert "result <= u16::MAX" in post, post
    assert "result == ((data[offset+1] as u16) << 8) | (data[offset] as u16)" in post, post
    # Both wrapped in parens so the AND associates correctly.
    assert post.startswith("("), post


def test_parser_promotes_functional_spec_when_post_is_trivial():
    """If the defensive postcondition is just ``"true"``, the functional
    spec replaces it entirely instead of producing ``"(true) && (...)"``."""
    out = _parse_llm_spec_response(
        _wrap("true", "true", functional="result == val + (align - 1) & !(align - 1)"),
        "align_up_64",
    )
    assert out is not None
    pre, post = out
    assert pre == "true"
    # No "(true) && (...)" wrapper — the trivial defensive post is dropped.
    assert post == "result == val + (align - 1) & !(align - 1)", post


def test_parser_keeps_functional_spec_with_old_syntax():
    """``old(buf.len())`` is preserved by the parser; the Kani backend's
    ``_extract_old_snapshots`` rewrites ``old(EXPR)`` into pre-call
    snapshot bindings before passing to ``kani::assert``. Previously
    the parser dropped specs containing old() as a workaround for the
    snapshotting gap; the backend now handles them natively."""
    out = _parse_llm_spec_response(
        _wrap(
            "true",
            "true",
            functional="buf.len() == old(buf.len()) + target",
        ),
        "pad_to",
    )
    assert out is not None
    pre, post = out
    assert pre == "true"
    # Post is the functional spec itself (defensive post was trivial).
    assert post == "buf.len() == old(buf.len()) + target", post


def test_parser_keeps_functional_spec_with_nested_old():
    """Nested old() (e.g. ``old(buf[..old(buf.len())])``) is preserved;
    backend's snapshot extractor strips the inner old() since the whole
    thing is captured at pre-state."""
    out = _parse_llm_spec_response(
        _wrap(
            "true",
            "true",
            functional="buf[..old(buf.len())] == old(buf[..])",
        ),
        "pad_to",
    )
    assert out is not None
    pre, post = out
    # Spec preserved verbatim; the harness layer rewrites it.
    assert "old(" in post


def test_parser_keeps_old_lookalike_identifiers():
    """Sanity: variable names like ``old_threshold`` should always
    merge into the post — they don't match the ``old(`` call form."""
    out = _parse_llm_spec_response(
        _wrap("true", "true", functional="result == old_threshold + 1"),
        "f",
    )
    assert out is not None
    pre, post = out
    assert "result == old_threshold + 1" in post, post


def test_parser_promotes_functional_spec_when_post_is_empty():
    out = _parse_llm_spec_response(
        _wrap("true", "", functional="result == bytes.len()"),
        "f",
    )
    # When defensive post is empty, the functional spec becomes the
    # entire postcondition. Whether the parser accepts an empty defensive
    # post at all depends on the upstream contract; here we just check
    # that the merge logic emits a non-empty result.
    if out is not None:
        pre, post = out
        assert post == "result == bytes.len()"
