"""Phase 1 harness-refiner (realism-enforcement plan, outcome C).

Tests the pure planning/synthesis functions: parse undefined externs, classify
boot-init-trusted globals defined in a sibling .c, and synthesize a conservative
materialization that can clean a NULL-deref artifact but never mask a real OOB.
"""

from bmc_agent.harness_refiner import (
    parse_undefined_externs,
    parse_null_defined_pointer_globals,
    classify_boot_init_trusted,
    synthesize_materialization,
    inject_materialization,
    plan_refinement,
    plan_refinement_null_defined,
    is_null_cex_value,
    globals_null_in_cex,
)


# --- CEx-witness gate: only refine a trusted global that is NULL in the trace ---

def test_is_null_cex_value_recognises_null_shapes():
    for v in ("NULL", "((uint32_t *)NULL)", "((const char *)NULL)",
              "0", "0u", "0ul", "(void *)0", "((void *)0)", " 0 "):
        assert is_null_cex_value(v), v


def test_is_null_cex_value_rejects_nonnull():
    for v in ("dynamic_object", "_buf_buf!0@1", "&x", "0x40000000",
              "1", "17472ul", "alloc_size!0@1", None, ""):
        assert not is_null_cex_value(v), v


def test_globals_null_in_cex_selects_only_null_pointers():
    va = {
        "fb_base": "((uint32_t *)NULL)",   # NULL  -> the artifact
        "mem_root": "dynamic_object",      # already materialized -> NOT artifact
        "fb_width": "0",                   # zero scalar (still "null-like")
        "other": "&g",
    }
    assert globals_null_in_cex(va, ["fb_base", "mem_root"]) == ["fb_base"]
    # a name absent from the CEx is not selected
    assert globals_null_in_cex(va, ["absent"]) == []
    assert globals_null_in_cex(None, ["fb_base"]) == []


# A sibling translation unit modelled on VibeOS vfs.c: a boot-init-trusted
# pointer global the FILE UNDER TEST itself defines as NULL (set only by
# vfs_init). The unit harness pulls this NULL definition in, so it LINKS CLEAN
# (no undefined reference) yet leaves mem_root NULL -> runtime NULL-deref.
_VFS_C = """
typedef struct vfs_node { struct vfs_node *children[8]; } vfs_node_t;
static vfs_node_t *mem_root = NULL;
void vfs_init(void) { mem_root = make_root(); }
vfs_node_t *vfs_lookup(const char *p) { return mem_root->children[0]; }
"""


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


# --- NULL-defined-in-harness detection (the link-succeeds artifact class) ---

_HARNESS_NULL_DEFINED = """\
#include <stdint.h>
typedef struct vfs_node { struct vfs_node *children[8]; } vfs_node_t;
/* file-scope var referenced by closure */
vfs_node_t *mem_root = NULL;
vfs_node_t *vfs_lookup(const char *p) { return mem_root->children[0]; }
int main(void){ vfs_lookup("/"); return 0; }
"""


def test_parse_null_defined_pointer_globals_finds_left_null():
    found = parse_null_defined_pointer_globals(_HARNESS_NULL_DEFINED)
    assert ("mem_root", "vfs_node_t *") in found


def test_parse_null_defined_excludes_reassigned_global():
    # If the harness already materializes it (g = calloc...), there is nothing to
    # refine — exclude it so the refiner does not claim a no-op demotion.
    harness = _HARNESS_NULL_DEFINED.replace(
        "int main(void){",
        "int main(void){ if(!mem_root){ mem_root = calloc(1,sizeof(*mem_root)); }",
    )
    assert parse_null_defined_pointer_globals(harness) == []


def test_parse_null_defined_ignores_scalars():
    harness = "#include <stdint.h>\nuint32_t fb_width = 0;\nint main(void){return 0;}\n"
    assert parse_null_defined_pointer_globals(harness) == []


def test_plan_refinement_null_defined_classifies_trusted():
    plan = plan_refinement_null_defined(_HARNESS_NULL_DEFINED, {"vfs.c": _VFS_C})
    assert [g.name for g in plan] == ["mem_root"]
    g = plan[0]
    assert g.already_defined is True
    assert g.is_pointer is True
    assert g.init_fn == "vfs_init"


def test_plan_refinement_null_defined_rejects_untrusted():
    # mem_root assigned outside *_init in siblings -> not trusted -> not refined.
    tainted = _VFS_C.replace(
        "vfs_node_t *vfs_lookup(const char *p) { return mem_root->children[0]; }",
        "void attacker(vfs_node_t *n) { mem_root = n; }",
    )
    assert plan_refinement_null_defined(_HARNESS_NULL_DEFINED, {"vfs.c": tainted}) == []


def test_synthesize_already_defined_reassigns_not_redefines():
    plan = plan_refinement_null_defined(_HARNESS_NULL_DEFINED, {"vfs.c": _VFS_C})
    block = synthesize_materialization(plan)
    # reassign-only: constructor + calloc, but NO redefinition of mem_root
    assert "calloc(1, sizeof(*mem_root))" in block
    assert "__attribute__((constructor))" in block
    assert "mem_root = (void*)0;" not in block


def test_inject_already_defined_appends_at_end():
    block = "/* x */\n#include <stdlib.h>\n__attribute__((constructor)) static void f(void){}\n"
    out = inject_materialization(_HARNESS_NULL_DEFINED, block, at_end=True)
    # the constructor lands after the global it reassigns is already defined
    assert out.index("mem_root = NULL;") < out.index("constructor")
    assert out.rstrip().endswith("}")
