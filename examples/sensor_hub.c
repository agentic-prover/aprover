/*
 * sensor_hub.c — Multi-sensor data aggregation hub (embedded)
 *
 * Call hierarchy:
 *
 *   process_sample(hub, sid, val)  →  record_reading(hub, sid, val)
 *   get_reading(hub, sid)          →  latest_value(hub, sid)
 *   (both entry functions also call clamp_sid inline)
 *
 * Generation layers:
 *   Layer 1: process_sample, get_reading   (entry points)
 *   Layer 2: record_reading, latest_value  (internals)
 *
 * Demonstration of GRACE refinement loop:
 *
 *   Phase 2 — CBMC (weak preconditions) finds two counterexamples:
 *
 *     (A) record_reading: assert(sid valid) fires (sid is unconstrained).
 *
 *     (B) latest_value: division by zero (n == 0) when count is unconstrained.
 *
 *   Phase 3 — CEx validation:
 *
 *     (A) SPURIOUS — process_sample guards the call with a direct comparison
 *           if (sid < 0 || sid >= NUM_SENSORS) return;
 *         This guard lives in the caller's own code (no callee stub involved),
 *         so when CBMC re-runs the reachability harness for process_sample it
 *         finds that the execution path
 *           (sid < 0 || sid >= NUM_SENSORS)  AND  reach record_reading
 *         is UNSATISFIABLE → counterexample is spurious.
 *         LLM refines precondition: adds "0 ≤ sid < NUM_SENSORS".
 *         Re-run CBMC with refined precondition → assert(sid valid) no longer fires.
 *         New CEx: sensor.count wraps uint8_t (255 → 0), assert(count > 0) fails.
 *         Reachability: process_sample never caps count, so hub with count==255
 *         is a valid caller state → REAL BUG confirmed.
 *
 *     (B) REAL BUG — get_reading calls latest_value without checking count > 0.
 *         Reachability: hub with sensors[sid].count == 0 is a valid input to
 *         get_reading (hub is not initialised by get_reading itself) → confirmed.
 */

#include <stdint.h>
#include <stddef.h>
#include <assert.h>

/* ------------------------------------------------------------------ */
/* Constants and data structures                                        */
/* ------------------------------------------------------------------ */

#define NUM_SENSORS 4
#define WINDOW      8   /* rolling-average window size */

typedef struct {
    int32_t readings[WINDOW]; /* circular window of recent samples    */
    uint8_t count;            /* total samples stored — wraps at 256! */
    int32_t sum;              /* sum of the last min(count,WINDOW) values */
    uint8_t slot;             /* next write position in readings[]    */
} sensor_t;

typedef struct {
    sensor_t sensors[NUM_SENSORS];
    uint16_t total_samples;
} hub_t;

/* ------------------------------------------------------------------ */
/* Internal helpers (Layer 2)                                          */
/* ------------------------------------------------------------------ */

/*
 * record_reading — append val to sensor sid's rolling window.
 *
 * Called by: process_sample
 *
 * BUG 1 (spurious with weak precondition):
 *   CBMC makes sid nondeterministic.  With precondition = true, sid can be
 *   −1 or 99, failing the assert below.  However, process_sample guards
 *   the call with a direct comparison  (sid < 0 || sid >= NUM_SENSORS)
 *   that does not go through any callee stub, so CBMC's reachability
 *   harness finds the path infeasible → spurious → precondition refined.
 *
 * BUG 2 (real, found after refinement):
 *   sensor.count is uint8_t.  After exactly 255 calls per sensor, the next
 *   increment wraps count to 0, and assert(s->count > 0) fires.
 *   process_sample places no upper bound on the call count, so a hub
 *   with count == 255 is a legitimate caller state → confirmed real bug.
 */
static void record_reading(hub_t *hub, int sid, int32_t val)
{
    /* BUG 1: fires when sid is out of range (spurious — caller guards) */
    assert(sid >= 0 && sid < NUM_SENSORS);

    sensor_t *s = &hub->sensors[sid];

    /* Evict oldest value once window is full */
    if (s->count >= WINDOW)
        s->sum -= s->readings[s->slot];

    /* Write new value */
    s->readings[s->slot] = val;
    s->slot = (uint8_t)((s->slot + 1) % WINDOW);
    s->sum += val;

    /* BUG 2: uint8_t wraps 255 → 0 after 255 samples */
    s->count++;
    assert(s->count > 0);
}

/*
 * latest_value — return the rolling average of stored readings.
 *
 * Called by: get_reading
 *
 * BUG 3 (real — division by zero):
 *   n == 0 when no readings have been stored yet.  get_reading never checks
 *   count > 0 before calling this function → division by zero is directly
 *   reachable from any caller that supplies a freshly-initialised hub.
 */
static int32_t latest_value(hub_t *hub, int sid)
{
    sensor_t *s = &hub->sensors[sid];
    uint8_t   n = (s->count < WINDOW) ? s->count : (uint8_t)WINDOW;

    /* BUG 3: n == 0 when count == 0 → division by zero */
    return s->sum / (int32_t)n;
}

/* ------------------------------------------------------------------ */
/* Public API (Layer 1 — entry points)                                  */
/* ------------------------------------------------------------------ */

/*
 * process_sample — validate sensor id inline, then record the new value.
 *
 * KEY: the guard  (sid < 0 || sid >= NUM_SENSORS)  is a direct comparison
 * in this function's body, not delegated to a callee.  This ensures that
 * CBMC's reachability harness (which stubs callees) still sees the
 * constraint, allowing it to conclude that record_reading is unreachable
 * with an invalid sid → spurious counterexample correctly detected.
 */
void process_sample(hub_t *hub, int sid, int32_t val)
{
    assert(hub != NULL);

    /* Direct bounds check — no callee stub involved */
    if (sid < 0 || sid >= NUM_SENSORS)
        return;

    record_reading(hub, sid, val);
    hub->total_samples++;
}

/*
 * get_reading — validate sensor id inline, return the rolling average.
 *
 * BUG: missing  if (hub->sensors[sid].count == 0) return 0;
 * Consequence: latest_value() divides by zero when the sensor has no data.
 */
int32_t get_reading(hub_t *hub, int sid)
{
    assert(hub != NULL);

    if (sid < 0 || sid >= NUM_SENSORS)
        return -1;

    /* Missing: if (hub->sensors[sid].count == 0) return 0; */
    return latest_value(hub, sid);
}
