"""plan_unwind_reduction — explosion-recovery unwind reduction on CBMC timeout.

A high unwind that times out is a state-space explosion (deep loop unrolling);
more time won't help, so we reduce the bound and re-run. Sound only because the
caller keeps --unwinding-assertions on (a loop exceeding the reduced bound fails
the unwinding assertion → refiner/unresolved, never a false clean).
"""

from bmc_agent.auto_retry_registry import plan_unwind_reduction


def test_high_unwind_is_reduced():
    # 34 (the vfs_lookup case) -> halve, capped to a tractable level.
    assert plan_unwind_reduction(34) == 8


def test_reduction_capped_to_tractable_level():
    # Even a huge unwind drops straight to the cap, not just halved.
    assert plan_unwind_reduction(64) == 8
    assert plan_unwind_reduction(256) == 8


def test_moderate_unwind_halves_below_cap():
    # 12 -> 6 (halved, below the cap of 8); still >= threshold default 16? No:
    # 12 < 16 default threshold, so None. Use an explicit lower threshold.
    assert plan_unwind_reduction(12, threshold=10) == 6


def test_low_unwind_not_reduced_default_threshold():
    # Below threshold => None (a low-unwind timeout is a near-miss → bump time).
    assert plan_unwind_reduction(4) is None
    assert plan_unwind_reduction(8) is None
    assert plan_unwind_reduction(15) is None


def test_at_threshold_is_reduced():
    assert plan_unwind_reduction(16) == 8


def test_floor_respected():
    # With a low threshold, a small unwind still never goes below the floor 4.
    assert plan_unwind_reduction(9, threshold=8) == 4   # 9//2=4, capped/floored
    # Can't reduce when halving wouldn't drop below current and floor.
    assert plan_unwind_reduction(5, threshold=4) == 4   # 5//2=2 -> floored to 4


def test_no_reduction_when_already_at_or_below_floor():
    # cur=4, threshold<=4: 4//2=2 -> floor 4 -> not < cur -> None.
    assert plan_unwind_reduction(4, threshold=1) is None


def test_disabled_returns_none():
    assert plan_unwind_reduction(34, enabled=False) is None
