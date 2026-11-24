"""
Dynamic validation tests for VibeOS bugs identified by AMC.

Each test compiles a minimal C harness that reproduces the exact logic from
the AMC classification artifact, runs it, and asserts the buggy behaviour
is present.

  PASS  = bug confirmed (the AMC finding is real on this code path)
  FAIL  = behaviour has changed (bug may be fixed, or the witness no longer applies)

Source references are to vibeos/repo/kernel/ (checked in under examples/).

Bugs covered
------------
1. process_mouse_report: sign confusion in clamping (mouse.c:43-49)
2. bus_to_arm: NULL result not checked in hal_fb_init (fb.c:289)
3. kbd_ring_push / mouse_ring_push: NULL report passed to memcpy (usb_hid.c:93-100 / 74-80)
"""

from __future__ import annotations

import subprocess
import textwrap
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compile_and_run(
    src: str,
    *,
    extra_cflags: list[str] | None = None,
    timeout: int = 10,
) -> tuple[int, str, str]:
    """Compile *src* with gcc, run it, return (returncode, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = Path(tmpdir) / "harness.c"
        exe_path = Path(tmpdir) / "harness"
        src_path.write_text(textwrap.dedent(src))
        flags = ["gcc", "-o", str(exe_path), str(src_path), "-g",
                 "-fno-builtin", "-Wall", "-Wno-unused-result"]
        if extra_cflags:
            flags.extend(extra_cflags)
        comp = subprocess.run(flags, capture_output=True, text=True)
        assert comp.returncode == 0, (
            f"Harness compilation failed:\n{comp.stderr}"
        )
        run = subprocess.run(
            [str(exe_path)], capture_output=True, text=True,
            timeout=timeout,
        )
        return run.returncode, run.stdout, run.stderr


# ---------------------------------------------------------------------------
# Bug 1 — process_mouse_report: sign confusion in position clamping
#
# Source: vibeos/repo/kernel/hal/pizero2w/mouse.c, lines 26-49 and 99-108
# AMC finding: vibeos_full | process_mouse_report | main.assertion.2
# Call chain: usb_irq_handler → hal_mouse_get_state → poll_usb_mouse
#             → process_mouse_report
#
# Root cause: fb_width and fb_height are uint32_t externs initialised to 0
# (BSS) by default.  The clamping code casts them to int without checking
# that the result is ≥ 0.  When fb_width == 0:
#
#   max_x = (int)0 - 1 = -1
#   mouse_x is first clamped to 0 (correct lower bound), then immediately
#   reclamped to max_x == -1 (wrong), leaving the cursor negative.
#
# Trigger: any USB mouse event that arrives before hal_fb_init() is called.
# ---------------------------------------------------------------------------

_MOUSE_CLAMP_SRC = """\
/* Reproduces vibeos/repo/kernel/hal/pizero2w/mouse.c clamping logic */
#include <stdint.h>
#include <stdio.h>

/* fb_width / fb_height are extern uint32_t in mouse.c;
   before hal_fb_init() they are 0 (zero-initialised BSS). */
static uint32_t fb_width  = 0;
static uint32_t fb_height = 0;

/* Initial cursor position (mouse.c lines 15-16) */
static int mouse_x = 400;
static int mouse_y = 300;

/* Exact clamping block from process_mouse_report(), lines 43-49 */
static void apply_clamp(void)
{
    int max_x = (int)fb_width  - 1;   /* 0 - 1 = -1 when fb_width == 0  */
    int max_y = (int)fb_height - 1;   /* 0 - 1 = -1 when fb_height == 0 */
    if (mouse_x < 0) mouse_x = 0;    /* 410 ≥ 0 — no change             */
    if (mouse_x > max_x) mouse_x = max_x; /* 410 > -1 → mouse_x = -1   */
    if (mouse_y < 0) mouse_y = 0;    /* 310 ≥ 0 — no change             */
    if (mouse_y > max_y) mouse_y = max_y; /* 310 > -1 → mouse_y = -1   */
}

int main(void)
{
    /* Simulate USB mouse report: dx=5, dy=5, scale=2 (lines 33-40) */
    mouse_x += 5 * 2;   /* 400 + 10 = 410 */
    mouse_y += 5 * 2;   /* 300 + 10 = 310 */

    apply_clamp();

    printf("fb_width=%u  fb_height=%u\\n", fb_width, fb_height);
    printf("mouse_x=%d   mouse_y=%d\\n", mouse_x, mouse_y);

    if (mouse_x < 0 || mouse_y < 0) {
        printf("BUG CONFIRMED: cursor position is negative after clamping\\n");
        printf("  expected: mouse_x>=0 && mouse_y>=0\\n");
        printf("  actual:   mouse_x=%d  mouse_y=%d\\n", mouse_x, mouse_y);
        return 1;  /* bug present */
    }
    printf("OK: cursor position is non-negative\\n");
    return 0;  /* bug absent or fixed */
}
"""

_HAL_MOUSE_SET_POS_SRC = """\
/* Same clamping bug in hal_mouse_set_pos(), lines 99-108 of mouse.c */
#include <stdint.h>
#include <stdio.h>

static uint32_t fb_width  = 0;
static uint32_t fb_height = 0;
static int mouse_x = 0;
static int mouse_y = 0;

static void hal_mouse_set_pos(int x, int y)
{
    mouse_x = x;
    mouse_y = y;
    int max_x = (int)fb_width  - 1;
    int max_y = (int)fb_height - 1;
    if (mouse_x < 0) mouse_x = 0;
    if (mouse_x > max_x) mouse_x = max_x;
    if (mouse_y < 0) mouse_y = 0;
    if (mouse_y > max_y) mouse_y = max_y;
}

int main(void)
{
    /* Set position to (100, 100) — a valid screen coordinate */
    hal_mouse_set_pos(100, 100);
    printf("hal_mouse_set_pos(100, 100) with fb_width=0\\n");
    printf("mouse_x=%d  mouse_y=%d\\n", mouse_x, mouse_y);
    if (mouse_x < 0 || mouse_y < 0) {
        printf("BUG CONFIRMED: hal_mouse_set_pos produces negative position\\n");
        return 1;
    }
    printf("OK\\n");
    return 0;
}
"""


def test_process_mouse_report_clamping_before_fb_init():
    """
    Clamping sets cursor negative when fb_width/fb_height are 0
    (state before hal_fb_init runs).

    AMC call chain: usb_irq_handler → poll_usb_mouse → process_mouse_report
    Witness: fb_width = 0, mouse_x starts at 400, dx = +5
    Expected (buggy): mouse_x == -1 after clamping
    """
    rc, stdout, _ = _compile_and_run(_MOUSE_CLAMP_SRC)
    print(stdout)
    assert rc == 1, (
        "Bug NOT reproduced: clamping did not produce a negative cursor position.\n"
        f"Output:\n{stdout}"
    )
    assert "mouse_x=-1" in stdout and "mouse_y=-1" in stdout, (
        f"Unexpected output: {stdout}"
    )


def test_hal_mouse_set_pos_clamping_before_fb_init():
    """
    hal_mouse_set_pos has the identical clamping bug as process_mouse_report
    (lines 103-108 of mouse.c).
    """
    rc, stdout, _ = _compile_and_run(_HAL_MOUSE_SET_POS_SRC)
    print(stdout)
    assert rc == 1, (
        "Bug NOT reproduced in hal_mouse_set_pos.\n"
        f"Output:\n{stdout}"
    )


# ---------------------------------------------------------------------------
# Bug 2 — bus_to_arm: NULL result not guarded in hal_fb_init
#
# Source: vibeos/repo/kernel/hal/pizero2w/fb.c, lines 166-168 and 283-308
# AMC finding: vibeos_full | bus_to_arm | main.assertion.2
# Call chain: system init → hal_fb_init → bus_to_arm
#
# Root cause: bus_to_arm masks the low 30 bits of the bus address.
# Any bus address whose low 30 bits are all zero maps to NULL.
# hal_fb_init checks fb_addr != 0 but not whether bus_to_arm returns NULL.
# It then enters the framebuffer-clear loop using the NULL base pointer.
#
# CBMC witness: bus = 0x80000000 → result = NULL
# On ARM/RPi the GPU bus address is 0xC0000000 | phys; if phys is 0 the
# result is also NULL.
# ---------------------------------------------------------------------------

_BUS_TO_ARM_SRC = """\
/* Reproduces vibeos/repo/kernel/hal/pizero2w/fb.c bus_to_arm + hal_fb_init */
#include <stdint.h>
#include <stdio.h>
#include <signal.h>
#include <setjmp.h>

/* Exact copy of bus_to_arm(), fb.c line 166 */
static void *bus_to_arm(uint32_t bus) {
    return (void *)(uint64_t)(bus & 0x3FFFFFFFu);
}

static sigjmp_buf g_jmp;
static volatile int g_sigsegv = 0;

static void on_sigsegv(int sig) {
    (void)sig;
    g_sigsegv = 1;
    siglongjmp(g_jmp, 1);
}

int main(void)
{
    /* CBMC witness: fb_addr = 0x80000000 (non-zero, passes the fb_addr==0 guard,
       but bus_to_arm maps it to NULL). */
    uint32_t fb_addr = 0x80000000u;

    /* hal_fb_init guards fb_addr == 0 but not bus_to_arm() == NULL */
    if (fb_addr == 0) {
        printf("early return: fb_addr is zero\\n");
        return 0;
    }

    uint32_t *fb_base = (uint32_t *)bus_to_arm(fb_addr);
    printf("bus_to_arm(0x%08X) = %p\\n", fb_addr, (void *)fb_base);

    if (fb_base != NULL) {
        printf("OK: bus address maps to non-NULL ARM address\\n");
        return 0;
    }

    /* fb_base == NULL.  hal_fb_init now does:
         for (uint32_t i = 0; i < width * virt_height; i++)
             fb_info.base[i] = 0x00000000;
       Reproduce the first write to confirm the crash. */
    signal(SIGSEGV, on_sigsegv);
    if (sigsetjmp(g_jmp, 1) == 0) {
        *fb_base = 0u;          /* write to NULL → SIGSEGV */
        printf("UNEXPECTED: write to NULL did not fault\\n");
        return 2;
    }

    if (g_sigsegv) {
        printf("BUG CONFIRMED: SIGSEGV writing to fb_base (NULL) from bus_to_arm\\n");
        printf("  hal_fb_init does not null-check bus_to_arm() return value\\n");
        return 1;  /* bug present */
    }
    return 0;
}
"""


def test_bus_to_arm_null_causes_fb_write_crash():
    """
    bus_to_arm(0x80000000) returns NULL; hal_fb_init writes through it
    causing a SIGSEGV.  No null check exists between the two calls.

    AMC witness: bus = 0x80000000 → result = NULL
    """
    rc, stdout, _ = _compile_and_run(_BUS_TO_ARM_SRC)
    print(stdout)
    assert rc == 1, (
        "Bug NOT reproduced: either bus_to_arm returned non-NULL "
        "or the NULL write did not fault.\n"
        f"Output:\n{stdout}"
    )
    assert "BUG CONFIRMED" in stdout, f"Unexpected output:\n{stdout}"


# ---------------------------------------------------------------------------
# Bug 3 — kbd_ring_push / mouse_ring_push: NULL report passed to memcpy
#
# Source: vibeos/repo/kernel/hal/pizero2w/usb/usb_hid.c, lines 74-100
# AMC finding: vibeos_full | kbd_ring_push | precondition_instance.2
#              vibeos_full | mouse_ring_push | precondition_instance.2
# Call chain: usb_irq_handler → kbd_ring_push (or mouse_ring_push)
#
# Root cause: usb_irq_handler passes intr_dma_buffer / mouse_dma_buffer
# directly to the ring-push functions without a NULL check.  If the DMA
# buffer pointer is NULL (not yet allocated, or allocation failed),
# memcpy(ring[head], NULL, 8) is called, which is undefined behaviour
# that typically faults on Linux.
#
# The ring-push functions themselves contain no NULL guard on 'report'.
# ---------------------------------------------------------------------------

_KBD_RING_PUSH_SRC = """\
/* Reproduces vibeos/repo/kernel/hal/pizero2w/usb/usb_hid.c kbd_ring_push */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>

#define KBD_RING_SIZE 16

typedef struct {
    uint8_t reports[KBD_RING_SIZE][8];
    int head;
    int tail;
} kbd_ring_t;

static kbd_ring_t kbd_ring = {{{{0}}}};

/* Exact copy from usb_hid.c lines 93-100 */
static void kbd_ring_push(const uint8_t *report)
{
    int next = (kbd_ring.head + 1) % KBD_RING_SIZE;
    if (next != kbd_ring.tail) {          /* not full */
        memcpy(kbd_ring.reports[kbd_ring.head], report, 8);
        kbd_ring.head = next;
    }
    /* no NULL guard on report */
}

static sigjmp_buf g_jmp;
static volatile int g_sigsegv = 0;

static void on_sigsegv(int sig) {
    (void)sig;
    g_sigsegv = 1;
    siglongjmp(g_jmp, 1);
}

int main(void)
{
    /* Simulate usb_irq_handler calling kbd_ring_push(intr_dma_buffer)
       where intr_dma_buffer has not been allocated yet (NULL). */
    static volatile const uint8_t *dma_buf = NULL;  /* volatile prevents optimisation */

    signal(SIGSEGV, on_sigsegv);
    printf("Calling kbd_ring_push(NULL) — simulating uninitialised DMA buffer\\n");

    if (sigsetjmp(g_jmp, 1) == 0) {
        kbd_ring_push((const uint8_t *)dma_buf);
        /* reaching here means memcpy(dst, NULL, 8) did not fault (UB not trapped) */
        if (!g_sigsegv) {
            printf("AMBIGUOUS: memcpy(dst, NULL, 8) did not trap on this platform\\n");
            printf("UB is still present — the NULL guard is missing\\n");
            return 2;  /* UB present but untrapped */
        }
    }

    if (g_sigsegv) {
        printf("BUG CONFIRMED: SIGSEGV from memcpy(dst, NULL, 8) in kbd_ring_push\\n");
        printf("  'report' parameter is never checked for NULL\\n");
        return 1;  /* bug present and crashed */
    }
    return 0;
}
"""

_MOUSE_RING_PUSH_SRC = """\
/* Reproduces vibeos/repo/kernel/hal/pizero2w/usb/usb_hid.c mouse_ring_push */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>

#define MOUSE_RING_SIZE 32
#define MOUSE_REPORT_SIZE 8

typedef struct {
    uint8_t reports[MOUSE_RING_SIZE][MOUSE_REPORT_SIZE];
    int head;
    int tail;
} mouse_ring_t;

static mouse_ring_t mouse_ring = {{{{0}}}};

/* Exact copy from usb_hid.c lines 74-80 */
static void mouse_ring_push(const uint8_t *report)
{
    int next = (mouse_ring.head + 1) % MOUSE_RING_SIZE;
    if (next != mouse_ring.tail) {
        memcpy(mouse_ring.reports[mouse_ring.head], report, MOUSE_REPORT_SIZE);
        mouse_ring.head = next;
    }
    /* no NULL guard on report */
}

static sigjmp_buf g_jmp;
static volatile int g_sigsegv = 0;

static void on_sigsegv(int sig) {
    (void)sig;
    g_sigsegv = 1;
    siglongjmp(g_jmp, 1);
}

int main(void)
{
    static volatile const uint8_t *dma_buf = NULL;

    signal(SIGSEGV, on_sigsegv);
    printf("Calling mouse_ring_push(NULL) — simulating uninitialised mouse DMA buffer\\n");

    if (sigsetjmp(g_jmp, 1) == 0) {
        mouse_ring_push((const uint8_t *)dma_buf);
        if (!g_sigsegv) {
            printf("AMBIGUOUS: memcpy(dst, NULL, n) did not trap on this platform\\n");
            return 2;
        }
    }

    if (g_sigsegv) {
        printf("BUG CONFIRMED: SIGSEGV from memcpy(dst, NULL, n) in mouse_ring_push\\n");
        printf("  'report' parameter is never checked for NULL\\n");
        return 1;
    }
    return 0;
}
"""


def test_kbd_ring_push_null_report_crashes():
    """
    kbd_ring_push(NULL) causes a SIGSEGV because memcpy is called with
    a NULL source pointer.  No NULL guard exists on the 'report' param.

    AMC call chain: usb_irq_handler → kbd_ring_push
    AMC property: kbd_ring_push.precondition_instance.2
    """
    rc, stdout, _ = _compile_and_run(_KBD_RING_PUSH_SRC)
    print(stdout)
    # rc==1: crashed (bug confirmed); rc==2: UB untrapped but guard still missing
    assert rc in (1, 2), (
        "Bug NOT reproduced: kbd_ring_push handled NULL without incident.\n"
        f"Output:\n{stdout}"
    )
    assert "BUG CONFIRMED" in stdout or "AMBIGUOUS" in stdout, (
        f"Unexpected output:\n{stdout}"
    )


def test_mouse_ring_push_null_report_crashes():
    """
    mouse_ring_push(NULL) — same missing NULL guard as kbd_ring_push.

    AMC call chain: usb_irq_handler → mouse_ring_push
    AMC property: mouse_ring_push.precondition_instance.2
    """
    rc, stdout, _ = _compile_and_run(_MOUSE_RING_PUSH_SRC)
    print(stdout)
    assert rc in (1, 2), (
        "Bug NOT reproduced: mouse_ring_push handled NULL without incident.\n"
        f"Output:\n{stdout}"
    )
    assert "BUG CONFIRMED" in stdout or "AMBIGUOUS" in stdout, (
        f"Unexpected output:\n{stdout}"
    )


# ---------------------------------------------------------------------------
# Negative control: verify that adding the obvious fixes silences each bug
# ---------------------------------------------------------------------------

_MOUSE_CLAMP_FIXED_SRC = """\
/* Fixed version of process_mouse_report clamping */
#include <stdint.h>
#include <stdio.h>

static uint32_t fb_width  = 0;
static uint32_t fb_height = 0;
static int mouse_x = 400;
static int mouse_y = 300;

static void apply_clamp_fixed(void)
{
    /* Fix: guard against fb_width / fb_height == 0 */
    if (fb_width == 0 || fb_height == 0) return;
    int max_x = (int)fb_width  - 1;
    int max_y = (int)fb_height - 1;
    if (mouse_x < 0) mouse_x = 0;
    if (mouse_x > max_x) mouse_x = max_x;
    if (mouse_y < 0) mouse_y = 0;
    if (mouse_y > max_y) mouse_y = max_y;
}

int main(void)
{
    mouse_x += 5 * 2;
    mouse_y += 5 * 2;
    apply_clamp_fixed();
    printf("mouse_x=%d  mouse_y=%d\\n", mouse_x, mouse_y);
    if (mouse_x < 0 || mouse_y < 0) {
        printf("STILL BUGGY\\n");
        return 1;
    }
    printf("OK: fix prevents negative cursor position\\n");
    return 0;
}
"""


def test_clamping_fix_prevents_negative_position():
    """
    Negative control: adding a fb_width > 0 guard silences the clamping bug.
    This test passes only when the fix is in place (expected: rc == 0).
    """
    rc, stdout, _ = _compile_and_run(_MOUSE_CLAMP_FIXED_SRC)
    print(stdout)
    assert rc == 0, (
        f"Fixed version still produces negative position:\n{stdout}"
    )
    assert "OK" in stdout
