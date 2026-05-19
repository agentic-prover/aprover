# claudes-c-compiler ir/analysis.rs — 1 real bug

**Date**: 2026-05-19
**Source**: anthropics/claudes-c-compiler `master` checkout 2026-05-19,
`src/ir/analysis.rs`
**Target functions**: 6 declared
**bmc-agent config**: Kani backend, `--threat-model security`. LLM:
`anthropic/claude-sonnet-4.5` via OpenRouter.

## Result

**1 real Rust panic confirmed in `intersect`** — the Cooper / Harvey /
Kennedy "find dominator-tree LCA" walker used during dominator-tree
construction.

### Bug: `intersect` — OOB on idom walk past `usize::MAX`

```rust
fn intersect(
    mut finger1: usize,
    mut finger2: usize,
    idom: &[usize],
    rpo_number: &[usize],
) -> usize {
    while finger1 != finger2 {
        while rpo_number[finger1] > rpo_number[finger2] {
            finger1 = idom[finger1];          // walk up the tree
            ...
        }
        ...
    }
    finger1
}
```

The walk `finger1 = idom[finger1]` is only safe if `idom[n]` is a
valid index for every `n` reached during the walk. The function's
precondition (LLM-inferred) checks this for the *entry* values
`finger1` and `finger2`, but **not for the iterates**.

If any node `n` on the walk has `idom[n] == usize::MAX` (the sentinel
for "no immediate dominator", typically the function's entry / root
block), the next iteration computes `idom[usize::MAX]` and panics
with `slice_index_fail`.

**Property hit**: `check_intersect.assertion.5` (slice index OOB).

## Why this is a real-but-LATENT-by-internal-invariant bug

A well-formed dominator tree maintains the invariant: walking up via
`idom` from any non-root node reaches the root before encountering
`usize::MAX`. The root's `idom` is itself or `usize::MAX`, and the
walk terminates before stepping past it.

If CCC's IR generator ever produces a malformed dominator tree (e.g.
during a refactor, or due to a bug in an earlier pass), `intersect`
crashes. The `pub fn` signature provides no protection.

**Classification**: REAL_BUG under `--threat-model security` (any
malformed-IR path could trigger it), LATENT under safety/functional
(no in-tree path produces a malformed dom tree in normal operation).

## Fix sketch

```rust
while finger1 != finger2 {
    while rpo_number.get(finger1).copied()
            > rpo_number.get(finger2).copied() {
        let next = idom.get(finger1).copied().ok_or(DomError::CycleOrMalformed)?;
        if next == finger1 || next == usize::MAX {
            return Err(DomError::WalkExceedsRoot);
        }
        finger1 = next;
    }
    ...
}
```

Use `idom.get(...)` rather than `idom[...]`, sentinel-check
`usize::MAX`, and detect self-loops (which would imply a malformed
tree).

## bmc-agent improvement landed

None ir/analysis.rs-specific. The bug surfaced via the standard
defensive spec workflow once the **LATENT classification + threat-
model gate** (commits `c3864af` + `975c1cd`) correctly promoted the
"no in-tree caller produces malformed idom" CEx from SPURIOUS to
REAL_BUG.
