"""Phase 1 harness-refiner (realism-enforcement plan, outcome C).

Tests the pure planning/synthesis functions: parse undefined externs, classify
boot-init-trusted globals defined in a sibling .c, and synthesize a conservative
materialization that can clean a NULL-deref artifact but never mask a real OOB.
"""

from bmc_agent.harness_refiner import (
    parse_undefined_externs,
    classify_boot_init_trusted,
    synthesize_materialization,
    inject_materialization,
    plan_refinement,
)


# A sibling translation unit modelled on VibeOS fb.c.
_FB_C = """
#include "fb.h"
uint32_t fb_width = 0;
uint32_t fb_height = 0;
uint32_t *fb_base = NULL;

void fb_init(fb_info_t *info) {
    fb_base = info->base;
    fb_width = info->width;
    fb_height = info->height;
}

void fb_put_pixel(uint32_t x, uint32_t y, uint32_t color) {
    if (x >= fb_width || y >= fb_height) return;
    fb_base[y * fb_width + x] = color;
}
"""

# A global that is NULL-init but ALSO assigned outside an *_init fn -> NOT trusted.
_TAINTED_C = """
char *cur_buf = NULL;
void handle_packet(char *p) { cur_buf = p; }
void net_init(void) { cur_buf = NULL; }
"""


def test_parse_undefined_externs():
    err = (
        "/usr/bin/ld: /tmp/x.o: in function `wsod_draw_line':\n"
        "x.c:(.text+0x12): undefined reference to `fb_base'\n"
        "x.c:(.text+0x20): undefined reference to `fb_width'\n"
        "x.c:(.text+0x20): undefined reference to `fb_base'\n"  # dup
    )
    assert parse_undefined_externs(err) == ["fb_base", "fb_width"]


def test_parse_undefined_externs_empty():
    assert parse_undefined_externs("") == []
    assert parse_undefined_externs(None) == []


def test_classify_pointer_global_is_trusted():
    tg = classify_boot_init_trusted("fb_base", {"fb.c": _FB_C})
    assert tg is not None
    assert tg.is_pointer is True
    assert tg.ctype.strip() == "uint32_t *"
    assert tg.init_fn == "fb_init"


def test_classify_scalar_global_is_trusted():
    tg = classify_boot_init_trusted("fb_width", {"fb.c": _FB_C})
    assert tg is not None
    assert tg.is_pointer is False
    assert tg.init_fn == "fb_init"


def test_classify_rejects_global_assigned_outside_init():
    # cur_buf is set by handle_packet (not an *_init fn) -> attacker-influenced.
    assert classify_boot_init_trusted("cur_buf", {"net.c": _TAINTED_C}) is None


def test_classify_unknown_global_is_none():
    assert classify_boot_init_trusted("does_not_exist", {"fb.c": _FB_C}) is None


def test_synthesize_materialization_pointer_callocs_one_element():
    tg = classify_boot_init_trusted("fb_base", {"fb.c": _FB_C})
    block = synthesize_materialization([tg])
    # conservative single-element model (cannot mask a real OOB)
    assert "calloc(1, sizeof(*fb_base))" in block
    assert "__attribute__((constructor))" in block
    assert "fb_base = (void*)0;" in block


def test_synthesize_empty():
    assert synthesize_materialization([]) == ""


def test_inject_materialization_after_last_include():
    harness = "#include <stdint.h>\n#include <stdlib.h>\nint main(void){return 0;}\n"
    out = inject_materialization(harness, "/*BLOCK*/\n")
    lines = out.splitlines()
    # block sits right after the last #include, before main
    inc_idx = max(i for i, l in enumerate(lines) if l.startswith("#include"))
    blk_idx = next(i for i, l in enumerate(lines) if "/*BLOCK*/" in l)
    main_idx = next(i for i, l in enumerate(lines) if "int main" in l)
    assert inc_idx < blk_idx < main_idx


def test_plan_refinement_end_to_end():
    err = (
        "undefined reference to `fb_base'\n"
        "undefined reference to `fb_width'\n"
        "undefined reference to `cur_buf'\n"
    )
    sibs = {"fb.c": _FB_C, "net.c": _TAINTED_C}
    plan = plan_refinement(err, sibs)
    names = sorted(g.name for g in plan)
    # fb_base + fb_width trusted; cur_buf rejected (tainted)
    assert names == ["fb_base", "fb_width"]


def test_plan_refinement_respects_referenced_filter():
    err = "undefined reference to `fb_base'\nundefined reference to `fb_width'\n"
    plan = plan_refinement(err, {"fb.c": _FB_C}, referenced_idents={"fb_base"})
    assert [g.name for g in plan] == ["fb_base"]
