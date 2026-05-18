/*
 * Test target for the bmc-agent-pr GitHub Action self-test.
 *
 * The baseline below is *clean* (bounds-checked). The accompanying
 * "buggy" branch removes the bounds check; the action's PR comment is
 * expected to surface that mutation as a REAL_BUG via
 * --signed-overflow-check / --unsigned-overflow-check selected by the
 * Phase 1.5 flag-selector.
 */

int safe_double(int x)
{
        if (x > 1000000 || x < -1000000)
                return 0;
        return x * 2;
}
