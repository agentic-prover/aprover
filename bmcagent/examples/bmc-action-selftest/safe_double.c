/*
 * Test target for the bmc-agent-pr GitHub Action self-test.
 *
 * BUGGY VARIANT — bounds check removed. For large x (close to INT_MAX),
 * x * 2 overflows signed int. The action's bmc-agent should pick
 * --signed-overflow-check in Phase 1.5 and surface this as REAL_BUG.
 */

int safe_double(int x)
{
        return x * 2;
}
