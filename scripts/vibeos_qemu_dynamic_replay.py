#!/usr/bin/env python3
"""Minimal VibeOS QEMU replay adapter for BMC-Agent dynamic validation.

This is deliberately target-specific and opt-in. BMC-Agent passes metadata via
``BMC_AGENT_DYN_QEMU_METADATA``; this adapter selects from a small catalog of
predeclared replay rules, builds a temporary VibeOS worktree, boots it under
QEMU, and emits marker lines consumed by ``DynamicValidator``.

The catalog is intentionally rule-oriented rather than finding-oriented:
entries are representative API/safety classes such as "public API writes to a
caller-provided pointer" or "dimension arithmetic overflows before bounds are
checked". It should not be expanded by copying benchmark result rows one by
one. See ``experiments/vibeos_qemu_validation/ADMISSION_PROTOCOL.md`` before
adding a new rule.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ADMISSION_PROTOCOL_VERSION = "v0.1"
DEFAULT_DISK_SIZE_MB = 64
DEFAULT_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)


@dataclass(frozen=True)
class ReplayCase:
    case: str
    entry_function: str
    category: str
    selection_rule: str
    target_event: str | None = None


REPLAY_CATALOG = {
    "net_get_mac_null": ReplayCase(
        case="net_get_mac_null",
        entry_function="net_get_mac",
        category="public_api_pointer_guard",
        selection_rule="public API writes to caller-provided output buffer",
        target_event="UNGUARDED_NULL_POINTER",
    ),
    "kapi_file_size_invalid_ptr": ReplayCase(
        case="kapi_file_size_invalid_ptr",
        entry_function="kapi_file_size",
        category="public_api_handle_guard",
        selection_rule="public API accepts an opaque handle and dereferences it after only a NULL check",
        target_event="UNGUARDED_INVALID_POINTER",
    ),
    "kapi_delete_invalid_path": ReplayCase(
        case="kapi_delete_invalid_path",
        entry_function="kapi_delete",
        category="public_api_string_guard",
        selection_rule="public API accepts a caller-provided string path and reaches path parsing without validating pointer accessibility",
        target_event="UNGUARDED_INVALID_STRING_POINTER",
    ),
    "kapi_rename_invalid_path": ReplayCase(
        case="kapi_rename_invalid_path",
        entry_function="kapi_rename",
        category="public_api_string_guard",
        selection_rule="public API accepts caller-provided string paths and reaches path parsing without validating pointer accessibility",
        target_event="UNGUARDED_INVALID_STRING_POINTER",
    ),
    "kapi_get_datetime_invalid_ptr": ReplayCase(
        case="kapi_get_datetime_invalid_ptr",
        entry_function="kapi_get_datetime",
        category="public_api_pointer_guard",
        selection_rule="public API writes to caller-provided output pointers after only NULL checks",
        target_event="UNGUARDED_INVALID_POINTER",
    ),
    "mouse_set_pos_large_coordinate": ReplayCase(
        case="mouse_set_pos_large_coordinate",
        entry_function="mouse_set_pos",
        category="unchecked_coordinate_arithmetic",
        selection_rule="public mouse-position API multiplies caller-provided coordinates before bounding them",
        target_event="SIGNED_INTEGER_OVERFLOW_UNGUARDED",
    ),
    "malloc_size_wrap": ReplayCase(
        case="malloc_size_wrap",
        entry_function="malloc",
        category="allocator_size_wrap",
        selection_rule="allocator aligns caller-provided size with arithmetic that can wrap before range rejection",
        target_event="ALLOCATOR_SIZE_WRAP",
    ),
    "vfs_read_null_file_data": ReplayCase(
        case="vfs_read_null_file_data",
        entry_function="vfs_read",
        category="unchecked_file_buffer",
        selection_rule="VFS read trusts a file node's data pointer before copying into the caller buffer",
        target_event="UNGUARDED_NULL_FILE_DATA",
    ),
    "hal_dma_fb_copy_overflow": ReplayCase(
        case="hal_dma_fb_copy_overflow",
        entry_function="hal_dma_fb_copy",
        category="dimension_arithmetic_overflow",
        selection_rule="framebuffer copy computes dimensions before rejecting overflowing geometry",
        target_event="SEMANTIC_MISMATCH",
    ),
}

CASE_BY_ENTRY = {rule.entry_function: rule.case for rule in REPLAY_CATALOG.values()}


GENERATED_ANCHORS = {
    "after_hal_dma_init": "    hal_dma_init();\n",
    "after_net_init": "    net_init();\n",
    "after_vfs_init": "    vfs_init();\n",
    "after_kapi_init_log": '    printf("[KERNEL] Kernel API initialized\\n");\n',
    "after_process_init": "    process_init();\n",
}

BANNED_INJECTION_PATTERNS = (
    "#include",
    "system(",
    "popen(",
    "fork(",
    "execve(",
    "execl(",
    "execv(",
    "extern ",
    "while (1)",
    "while(1)",
    "for (;;)",
    "for(;;)",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("BMC_AGENT_VIBEOS_REPO", ""))
    parser.add_argument(
        "--case",
        default=os.environ.get("BMC_AGENT_VIBEOS_REPLAY_CASE", "auto"),
        choices=["auto", "boot_smoke", *sorted(REPLAY_CATALOG)],
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("BMC_AGENT_DYN_QEMU_WORKDIR", ""),
    )
    parser.add_argument("--build-timeout", type=int, default=180)
    parser.add_argument("--qemu-timeout", type=int, default=20)
    parser.add_argument("--qemu-bin", default=os.environ.get("BMC_AGENT_VIBEOS_QEMU_BIN", "qemu-system-aarch64"))
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument(
        "--with-fat32-disk",
        action="store_true",
        default=os.environ.get("BMC_AGENT_VIBEOS_QEMU_FAT32_DISK", "").lower() in {"1", "true", "yes", "on"},
        help="Attach a generated FAT32 virtio-blk disk with a TTF font resource",
    )
    parser.add_argument(
        "--font-file",
        default=os.environ.get("BMC_AGENT_VIBEOS_FONT_FILE", ""),
        help="TTF file to expose as /fonts/Roboto/Roboto-Regular.ttf on the generated disk",
    )
    parser.add_argument(
        "--llm-generate-unsupported",
        action="store_true",
        default=os.environ.get("BMC_AGENT_VIBEOS_LLM_GENERATE_UNSUPPORTED", "").lower() in {"1", "true", "yes", "on"},
        help="Generate a bounded kernel.c replay with the LLM when --case auto has no catalog match",
    )
    parser.add_argument(
        "--secret-env",
        default=os.environ.get("BMC_AGENT_VIBEOS_SECRET_ENV", "/mnt/disk7/jw_bmc/secrets/openrouter.env"),
        help="Optional env file for the OpenRouter key; values are loaded without printing them",
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("BMC_AGENT_LLM_MODEL", "anthropic/claude-sonnet-4.6"),
        help="Model used for generated replay plans when env does not already override it",
    )
    args = parser.parse_args(argv)

    if args.list_cases:
        print(json.dumps(_catalog_manifest(), indent=2, sort_keys=True), flush=True)
        return 0

    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    if repo is None or not repo.exists():
        print("VALIDATION:INCONCLUSIVE missing VibeOS repo", flush=True)
        return 2

    workdir = Path(args.workdir).resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="vibeos-dyn-"))
    workdir.mkdir(parents=True, exist_ok=True)
    metadata = _load_metadata()
    case = _resolve_case(args.case, metadata)
    if not case:
        if not args.llm_generate_unsupported:
            _emit_marker(workdir, f"VALIDATION:INCONCLUSIVE unsupported VibeOS replay entry: {metadata.get('entry_function', '')}")
            return 3
        case = "llm_generated"
    replay_rule = REPLAY_CATALOG.get(case)

    replay_root = workdir / "vibeos_replay"
    if replay_root.exists():
        shutil.rmtree(replay_root)
    print(f"[BMC-DYN] VibeOS replay case={case}", flush=True)
    if replay_rule:
        print(f"[BMC-DYN] replay category={replay_rule.category}", flush=True)
        print(f"[BMC-DYN] selection rule={replay_rule.selection_rule}", flush=True)
    _write_json(workdir / "replay_metadata.json", _replay_metadata(case, replay_rule, metadata))
    print(f"[BMC-DYN] source repo={repo}", flush=True)
    print(f"[BMC-DYN] worktree={replay_root}", flush=True)

    generated_plan = None
    if case == "llm_generated":
        plan_result = _generate_llm_replay_plan(metadata, repo, workdir, args)
        if not plan_result.get("accepted"):
            reason = plan_result.get("error") or "no generated replay plan"
            prefix = "LLM marked unsupported" if plan_result.get("unsupported") else "LLM replay unavailable"
            _emit_marker(workdir, f"VALIDATION:INCONCLUSIVE {prefix}: {reason}")
            return 4
        generated_plan = plan_result["plan"]

    _ensure_tlse_submodule(repo)
    _copy_vibeos_repo(repo, replay_root)
    try:
        if case == "llm_generated":
            _inject_generated_replay(replay_root, generated_plan, workdir)
        elif case != "boot_smoke":
            _inject_replay(replay_root, case)
    except Exception as exc:
        _emit_marker(workdir, f"VALIDATION:INCONCLUSIVE replay injection failed: {exc}")
        _cleanup(replay_root, keep=args.keep_worktree)
        return 4

    disk_img = _prepare_qemu_disk_image(replay_root, workdir, args)

    build = _run(_vibeos_build_command(enable_replay=case != "boot_smoke"), cwd=replay_root, timeout=args.build_timeout)
    _write_text(workdir / "vibeos_build.stdout.log", build.stdout)
    _write_text(workdir / "vibeos_build.stderr.log", build.stderr)
    if build.returncode != 0:
        print("[BMC-DYN] VibeOS build failed", flush=True)
        print(_tail(build.stdout + "\n" + build.stderr), flush=True)
        _cleanup(replay_root, keep=args.keep_worktree)
        return 5

    kernel_bin = replay_root / "build" / "vibeos.bin"
    print(f"[BMC-DYN] VibeOS build OK: {kernel_bin}", flush=True)
    if args.build_only:
        _emit_marker(workdir, "VALIDATION:INCONCLUSIVE build-only mode did not execute target")
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0

    qemu_bin = shutil.which(args.qemu_bin)
    if not qemu_bin:
        _emit_marker(workdir, f"VALIDATION:INCONCLUSIVE qemu binary not found: {args.qemu_bin}")
        _cleanup(replay_root, keep=args.keep_worktree)
        return 6

    qemu = _run(_qemu_command(qemu_bin, kernel_bin, disk_img), cwd=replay_root, timeout=args.qemu_timeout)
    combined = qemu.stdout + "\n" + qemu.stderr
    _write_text(workdir / "vibeos_qemu.stdout.log", qemu.stdout)
    _write_text(workdir / "vibeos_qemu.stderr.log", qemu.stderr)
    print(_tail(combined, limit=12000), flush=True)

    embedded_marker = _first_marker_line(combined)
    if embedded_marker and embedded_marker.startswith("DYNAMIC:"):
        invalid = _invalid_confirmed_marker_reason(embedded_marker)
        if invalid:
            _emit_marker(workdir, f"VALIDATION:INCONCLUSIVE invalid generated confirmation marker: {invalid}")
            _cleanup(replay_root, keep=args.keep_worktree)
            return 0
        _record_marker(workdir, embedded_marker)
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0
    if _has_target_fault(combined):
        _emit_marker(workdir, "DYNAMIC:CONFIRMED target_event=TARGET_PANIC")
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0
    if case == "boot_smoke" and "[BOOT] Kernel loaded successfully!" in combined:
        _emit_marker(workdir, "VALIDATION:PASS boot_smoke reached VibeOS boot marker")
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0
    if f"[BMC-DYN] case {case} no crash" in combined:
        target_event = _observed_safety_event(case)
        if target_event:
            _emit_marker(
                workdir,
                f"DYNAMIC:OBSERVED_SAFETY_CONCERN target_event={target_event} "
                f"replay case {case} reached unsafe target call without "
                "a VibeOS-visible fault",
            )
        else:
            _emit_marker(
                workdir,
                f"VALIDATION:INCONCLUSIVE target_event=NO_TARGET_FAULT "
                f"replay case {case} reached no-crash marker",
            )
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0

    _emit_marker(workdir, f"VALIDATION:INCONCLUSIVE replay case {case} emitted no target verdict marker")
    _cleanup(replay_root, keep=args.keep_worktree)
    return 7


def _load_metadata() -> dict:
    path = os.environ.get("BMC_AGENT_DYN_QEMU_METADATA", "")
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_case(case: str, metadata: dict) -> str | None:
    if case != "auto":
        return case
    entry = str(metadata.get("entry_function") or os.environ.get("BMC_AGENT_DYN_QEMU_ENTRY") or "")
    return CASE_BY_ENTRY.get(entry)


def _replay_metadata(case: str, replay_rule: ReplayCase | None, metadata: dict) -> dict:
    payload = {
        "admission_protocol_version": ADMISSION_PROTOCOL_VERSION,
        "case": case,
        "metadata_entry_function": metadata.get("entry_function"),
        "metadata_failing_property": metadata.get("failing_property"),
    }
    if replay_rule:
        payload["replay_rule"] = asdict(replay_rule)
    else:
        payload["replay_rule"] = None
    return payload


def _catalog_manifest() -> list[dict]:
    return [
        {
            "admission_protocol_version": ADMISSION_PROTOCOL_VERSION,
            **asdict(rule),
        }
        for rule in sorted(REPLAY_CATALOG.values(), key=lambda item: item.case)
    ]


def _ensure_tlse_submodule(repo: Path) -> None:
    if (repo / "vendor" / "tlse" / "tlse.c").exists() or not (repo / ".git").exists():
        return
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _copy_vibeos_repo(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns(".git", "build", "disk.img", "*.o", "*.elf", "*.img", "__pycache__")
    shutil.copytree(src, dst, ignore=ignore)


def _inject_replay(repo: Path, case: str) -> None:
    kernel_c = repo / "kernel" / "kernel.c"
    text = kernel_c.read_text(encoding="utf-8")
    if case == "net_get_mac_null":
        anchor = "    net_init();\n"
        injection = """
    printf("[BMC-DYN] case net_get_mac_null start\\n");
    net_get_mac((uint8_t *)0);
    printf("[BMC-DYN] case net_get_mac_null no crash\\n");
"""
    elif case == "kapi_file_size_invalid_ptr":
        anchor = '    printf("[KERNEL] Kernel API initialized\\n");\n'
        injection = """
    printf("[BMC-DYN] case kapi_file_size_invalid_ptr start\\n");
    volatile int _bmc_dyn_file_size = kapi.file_size((void *)0xffffffffffffffffULL);
    printf("[BMC-DYN] case kapi_file_size_invalid_ptr no crash result=%d\\n", _bmc_dyn_file_size);
"""
    elif case == "kapi_delete_invalid_path":
        anchor = '    printf("[KERNEL] Kernel API initialized\\n");\n'
        injection = """
    printf("[BMC-DYN] case kapi_delete_invalid_path start\\n");
    volatile int _bmc_dyn_delete = kapi.delete((const char *)0xffffffffffffffffULL);
    printf("[BMC-DYN] case kapi_delete_invalid_path no crash result=%d\\n", _bmc_dyn_delete);
"""
    elif case == "kapi_rename_invalid_path":
        anchor = '    printf("[KERNEL] Kernel API initialized\\n");\n'
        injection = """
    printf("[BMC-DYN] case kapi_rename_invalid_path start\\n");
    volatile int _bmc_dyn_rename = kapi.rename((const char *)0xffffffffffffffffULL, "bmc_dyn");
    printf("[BMC-DYN] case kapi_rename_invalid_path no crash result=%d\\n", _bmc_dyn_rename);
"""
    elif case == "kapi_get_datetime_invalid_ptr":
        anchor = '    printf("[KERNEL] Kernel API initialized\\n");\n'
        injection = """
    printf("[BMC-DYN] case kapi_get_datetime_invalid_ptr start\\n");
    kapi.get_datetime((int *)0xffffffffffffffffULL, 0, 0, 0, 0, 0, 0);
    printf("[BMC-DYN] case kapi_get_datetime_invalid_ptr no crash\\n");
"""
    elif case == "mouse_set_pos_large_coordinate":
        anchor = '    printf("[KERNEL] Kernel API initialized\\n");\n'
        injection = """
    printf("[BMC-DYN] case mouse_set_pos_large_coordinate start\\n");
    mouse_set_pos(65536, 65536);
    printf("DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=SIGNED_INTEGER_OVERFLOW_UNGUARDED mouse_set_pos reached unchecked x*32768/y*32768 arithmetic\\n");
"""
    elif case == "malloc_size_wrap":
        anchor = '    printf("[KERNEL] Kernel API initialized\\n");\n'
        injection = """
    printf("[BMC-DYN] case malloc_size_wrap start\\n");
    void *_bmc_dyn_malloc = malloc((size_t)-8);
    if (_bmc_dyn_malloc) {
        printf("DYNAMIC:CONFIRMED target_event=ALLOCATOR_SIZE_WRAP malloc accepted wrapping huge allocation ptr=%p\\n", _bmc_dyn_malloc);
    } else {
        printf("[BMC-DYN] case malloc_size_wrap no mismatch malloc returned NULL\\n");
    }
"""
    elif case == "vfs_read_null_file_data":
        anchor = '    printf("[KERNEL] Kernel API initialized\\n");\n'
        injection = """
    printf("[BMC-DYN] case vfs_read_null_file_data start\\n");
    vfs_node_t _bmc_dyn_file;
    memset(&_bmc_dyn_file, 0, sizeof(_bmc_dyn_file));
    _bmc_dyn_file.type = VFS_FILE;
    _bmc_dyn_file.data = 0;
    _bmc_dyn_file.size = 1;
    char _bmc_dyn_buf[8] = {0};
    volatile int _bmc_dyn_read = vfs_read(&_bmc_dyn_file, _bmc_dyn_buf, 1, 0);
    (void)_bmc_dyn_read;
    printf("DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=UNGUARDED_NULL_FILE_DATA vfs_read reached memcpy from NULL file data without rejecting node\\n");
"""
    elif case == "hal_dma_fb_copy_overflow":
        anchor = "    hal_dma_init();\n"
        injection = """
    printf("[BMC-DYN] case hal_dma_fb_copy_overflow start\\n");
    uint32_t _bmc_dyn_dma_src[8] = {
        0x11111111u, 0x22222222u, 0x33333333u, 0x44444444u,
        0x55555555u, 0x66666666u, 0x77777777u, 0x88888888u
    };
    uint32_t _bmc_dyn_dma_dst[8] = {0};
    hal_dma_fb_copy(_bmc_dyn_dma_dst, _bmc_dyn_dma_src, 0x40000001u, 4u);
    if (_bmc_dyn_dma_dst[0] == 0x11111111u &&
        _bmc_dyn_dma_dst[1] == 0x22222222u &&
        _bmc_dyn_dma_dst[2] == 0x33333333u &&
        _bmc_dyn_dma_dst[3] == 0x44444444u &&
        _bmc_dyn_dma_dst[4] == 0u) {
        printf("DYNAMIC:CONFIRMED target_event=SEMANTIC_MISMATCH hal_dma_fb_copy overflow truncated copy\\n");
    } else {
        printf("[BMC-DYN] case hal_dma_fb_copy_overflow no mismatch dst4=0x%x\\n", _bmc_dyn_dma_dst[4]);
    }
"""
    else:
        raise ValueError(f"unsupported VibeOS replay case: {case}")
    if injection.strip() in text:
        return
    if anchor not in text:
        raise RuntimeError(f"could not find VibeOS injection anchor for case {case}")
    kernel_c.write_text(text.replace(anchor, anchor + _guard_replay_injection(injection), 1), encoding="utf-8")


def _generate_llm_replay_plan(metadata: dict, repo: Path, workdir: Path, args: argparse.Namespace) -> dict:
    _prepare_llm_environment(args)
    prompt = _build_llm_replay_prompt(metadata, repo)
    _write_text(workdir / "llm_prompt.txt", prompt)
    try:
        from bmc_agent.config import Config
        from bmc_agent.llm import LLMClient

        cfg = Config.from_env()
        llm = LLMClient(cfg)
        response = llm.complete(
            "You generate bounded C replay injections for VibeOS QEMU validation. Return only JSON.",
            prompt,
            max_tokens=2048,
            temperature=0.1,
            role="dynamic_repro",
        )
    except Exception as exc:
        return {"accepted": False, "error": _sanitize_error(str(exc))[:500]}

    _write_text(workdir / "llm_response.txt", response or "")
    try:
        plan = _parse_json_object(response)
    except Exception as exc:
        return {"accepted": False, "error": f"invalid JSON: {exc}"}

    _write_json(workdir / "llm_plan.raw.json", plan)
    if plan.get("supported") is False:
        return {
            "accepted": False,
            "unsupported": True,
            "error": str(plan.get("reason") or "LLM marked unsupported"),
        }
    ok, error = _validate_llm_plan(plan)
    if not ok:
        return {"accepted": False, "error": error or "plan rejected"}
    _write_json(workdir / "llm_plan.accepted.json", plan)
    return {"accepted": True, "plan": plan}


def _build_llm_replay_prompt(metadata: dict, repo: Path) -> str:
    function = str(metadata.get("entry_function") or "")
    source_context = (
        _extract_source_context(repo, metadata.get("source_file"), function)
        + "\n\n"
        + _extract_common_public_api_context(repo)
    )[:14000]
    finding_report = _read_optional_text(metadata.get("finding_path"), limit=8000)
    witness = _format_variable_assignments(metadata.get("variable_assignments") or {})
    caller_path = " -> ".join(str(item) for item in metadata.get("caller_path") or [])
    anchor_list = "\n".join(f"- {name}: insert immediately after `{anchor.strip()}`" for name, anchor in GENERATED_ANCHORS.items())
    return f"""Generate one VibeOS QEMU target replay plan for an existing BMC-Agent finding.

The tool will copy the VibeOS repo, patch only `kernel/kernel.c`, build with
`make TARGET=qemu PRINTF=uart`, boot with QEMU, and parse serial output markers.
When enabled by the runner, QEMU also gets a generated FAT32 virtio-blk disk
containing `/fonts/Roboto/Roboto-Regular.ttf` and `/home/user`.

Plan policy:
- First decide whether this finding can be honestly exercised on TARGET=qemu
  using only code visible from `kernel/kernel.c` and its existing includes.
- If the target path depends on Pi-only hardware, QEMU HAL stubs, hidden static
  state, unavailable device queues, unavailable user programs, or an
  environment condition you cannot create from `kernel_main`, return
  `"supported": false`.
- If a function is static/internal but reachable through a public API after
  `vfs_init()` or `ttf_init()`, use the public API path instead of calling the
  static/internal function directly.
- Do not invent declarations with `extern` or modify private global state to
  force reachability. If a function or state is not visible through existing
  headers, the replay is unsupported.
- It is acceptable to generate a replay that ends in
  `VALIDATION:INCONCLUSIVE target_event=NO_TARGET_VERDICT ...` when the target
  returns a guard/error value or the unsafe state is absent on QEMU.

Hard safety rules:
- Return only JSON. Do not include markdown.
- You may only patch `kernel/kernel.c`.
- `anchor` must be one of the allowed anchor labels below.
- `c_injection` must be C statements inserted inside `kernel_main`, not a full file.
- Do not use shell, filesystem host paths, `#include`, infinite loops, or build commands.
- Prefer public APIs and headers already included by `kernel/kernel.c`.
- Only call functions, types, and macros that are declared in the provided
  source/header context or in headers already included by `kernel/kernel.c`;
  do not guess helper API names. If the setup API you need is not shown, return
  `"supported": false`.
- Do not call `stbtt_*` functions directly from `kernel_main`; they are
  internal to `kernel/ttf.c`. Use `ttf_init`, `ttf_get_glyph`,
  `ttf_get_metrics`, `ttf_get_advance`, or `ttf_get_kerning`.
- Do not call private `kapi_*` helper functions directly. Use public kernel
  APIs such as `vfs_create`, `vfs_lookup`, `vfs_read`, `vfs_write`, and
  `vfs_append`, or use the `kapi.<field>` table after `kapi_init`.
- If the function is static/internal or cannot be honestly exercised on TARGET=qemu, set `"supported": false`.
- Print `[BMC-DYN] ...` diagnostics for setup, safe guards, and ordinary return values.
- If the replay reaches a concrete unsafe operation but VibeOS will not fault, print:
  `DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=<EVENT> <short detail>`
- If the replay can detect a semantic mismatch directly, print:
  `DYNAMIC:CONFIRMED target_event=<EVENT> <short detail>`
- Do not print `DYNAMIC:CONFIRMED` for a safe control path or for a call that
  merely returns without fault. Use `[BMC-DYN] ...` diagnostics for those paths.
- `DYNAMIC:CONFIRMED` is only for a target fault, a directly observed semantic
  mismatch, or a directly observed unsafe acceptance of invalid input.
- If the target returns an error/guard value such as `-1`, or the condition is
  absent on QEMU, print `VALIDATION:INCONCLUSIVE target_event=NO_TARGET_VERDICT ...`.
- Emit at most one verdict marker (`DYNAMIC:` or `VALIDATION:`) on any executed path.
- Verdict markers must be emitted by `printf("DYNAMIC:...\\n")` or
  `printf("VALIDATION:...\\n")`; never write raw `DYNAMIC:` or `VALIDATION:`
  tokens as C statements.
- If the target is expected to panic/data-abort before your marker, print a `[BMC-DYN] case ... start` line first; the tool will classify target panic.

Allowed anchors:
{anchor_list}

JSON schema:
{{
  "supported": true,
  "reason": "short reason",
  "patch_file": "kernel/kernel.c",
  "anchor": "after_kapi_init_log",
  "target_event": "UPPER_SNAKE_CASE_EVENT",
  "expected_outcome": "qemu_confirmed or observed_safety_concern or inconclusive",
  "c_injection": "C statements to insert"
}}

If unsupported:
{{
  "supported": false,
  "reason": "why this cannot be replayed honestly on TARGET=qemu"
}}

Finding metadata:
- function: {function}
- source_file: {metadata.get('source_file')}
- violated_property: {metadata.get('failing_property')}
- caller_path: {caller_path or '(none)'}

Counterexample assignments:
```text
{witness}
```

Finding report, if available:
```markdown
{finding_report or '(none)'}
```

Relevant source context:
```c
{source_context}
```
"""


def _extract_source_context(repo: Path, source_file: object, function: str) -> str:
    candidates: list[Path] = []
    if source_file:
        source_path = Path(str(source_file))
        candidates.append(source_path)
        if not source_path.is_absolute():
            candidates.append(repo / source_path)
        candidates.append(repo / source_path.name)
        candidates.append(repo / "kernel" / source_path.name)
    if function:
        for path in (repo / "kernel").rglob("*.c"):
            if path in candidates:
                continue
            try:
                if function in path.read_text(encoding="utf-8", errors="replace"):
                    candidates.append(path)
                    break
            except OSError:
                continue

    path = next((p for p in candidates if p.exists() and p.is_file()), None)
    if not path:
        return ""
    header_text = ""
    header = path.with_suffix(".h")
    if header.exists() and header.is_file():
        header_text = header.read_text(encoding="utf-8", errors="replace")[:5000]
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    candidates_idx = [i for i, line in enumerate(lines) if function and function in line]
    if not candidates_idx:
        source_text = text[:9000]
        if header_text:
            try:
                header_name = header.relative_to(repo)
            except ValueError:
                header_name = header.name
            return f"/* Header: {header_name} */\n{header_text}\n\n/* Source: {path.name} */\n{source_text}"
        return source_text
    idx = candidates_idx[0]
    for i in candidates_idx:
        window = "\n".join(lines[i:min(len(lines), i + 4)])
        if "{" in window and not lines[i].lstrip().startswith("//"):
            idx = i
            break
    start = max(0, idx - 80)
    end = min(len(lines), idx + 140)
    source_text = "\n".join(f"{n + 1}: {lines[n]}" for n in range(start, end))
    if header_text:
        try:
            header_name = header.relative_to(repo)
        except ValueError:
            header_name = header.name
        return f"/* Header: {header_name} */\n{header_text}\n\n/* Source excerpt: {path.name} */\n{source_text}"
    return source_text


def _extract_common_public_api_context(repo: Path) -> str:
    chunks = []
    for rel in ("kernel/vfs.h", "kernel/ttf.h", "kernel/kapi.h"):
        path = repo / rel
        if path.exists() and path.is_file():
            chunks.append(f"/* Public header: {rel} */\n{path.read_text(encoding='utf-8', errors='replace')[:5000]}")
    return "\n\n".join(chunks)


def _read_optional_text(path_value: object, *, limit: int) -> str:
    if not path_value:
        return ""
    path = Path(str(path_value))
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _format_variable_assignments(assignments: object) -> str:
    if not isinstance(assignments, dict) or not assignments:
        return "(none)"
    lines = []
    for key, value in assignments.items():
        if str(key).startswith("__CPROVER_"):
            continue
        lines.append(f"{key} = {value}")
        if len(lines) >= 80:
            lines.append("...")
            break
    return "\n".join(lines) if lines else "(none)"


def _parse_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("top-level JSON is not an object")
    return value


def _validate_llm_plan(plan: dict) -> tuple[bool, str | None]:
    if plan.get("supported") is not True:
        return False, str(plan.get("reason") or "LLM marked unsupported")
    if plan.get("patch_file") != "kernel/kernel.c":
        return False, "patch_file must be kernel/kernel.c"
    anchor = plan.get("anchor")
    if anchor not in GENERATED_ANCHORS:
        return False, f"unsupported anchor: {anchor}"
    injection = plan.get("c_injection")
    if not isinstance(injection, str) or not injection.strip():
        return False, "missing c_injection"
    if len(injection) > 6000:
        return False, "c_injection too large"
    lowered = injection.lower()
    for banned in BANNED_INJECTION_PATTERNS:
        if banned.lower() in lowered:
            return False, f"banned token in c_injection: {banned}"
    if "[BMC-DYN]" not in injection and "DYNAMIC:" not in injection:
        return False, "c_injection must emit a BMC-DYN or DYNAMIC marker"
    unquoted_marker = _invalid_unquoted_marker_reason(injection)
    if unquoted_marker:
        return False, unquoted_marker
    invalid = _invalid_confirmed_marker_reason(injection)
    if invalid:
        return False, invalid
    invalid_call = _invalid_generated_call_reason(injection)
    if invalid_call:
        return False, invalid_call
    return True, None


def _inject_generated_replay(repo: Path, plan: dict, workdir: Path) -> None:
    kernel_c = repo / "kernel" / "kernel.c"
    before = kernel_c.read_text(encoding="utf-8")
    anchor = GENERATED_ANCHORS[plan["anchor"]]
    if anchor not in before:
        raise RuntimeError(f"could not find VibeOS injection anchor: {plan['anchor']}")
    injection = _guard_replay_injection(plan["c_injection"])
    after = before.replace(anchor, anchor + injection, 1)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="kernel/kernel.c.before",
            tofile="kernel/kernel.c.after",
        )
    )
    _write_text(workdir / "generated_replay.diff", diff)
    kernel_c.write_text(after, encoding="utf-8")


def _guard_replay_injection(injection: str) -> str:
    body = injection.strip()
    return f"\n#ifdef BMC_DYN_REPLAY\n{body}\n#endif\n"


def _vibeos_build_command(*, enable_replay: bool) -> list[str]:
    cmd = ["make", "TARGET=qemu", "PRINTF=uart"]
    if enable_replay:
        cmd.append("CFLAGS_TARGET=-DTARGET_QEMU -DPRINTF_UART -DBMC_DYN_REPLAY")
    return cmd


def _prepare_qemu_disk_image(replay_root: Path, workdir: Path, args: argparse.Namespace) -> Path | None:
    if not getattr(args, "with_fat32_disk", False):
        return None
    font_file = _resolve_font_file(getattr(args, "font_file", "") or "")
    disk_img = replay_root / "disk.img"
    manifest = {
        "enabled": True,
        "disk_img": str(disk_img),
        "font_file": str(font_file) if font_file else None,
        "font_target_path": "/fonts/Roboto/Roboto-Regular.ttf" if font_file else None,
        "size_mb": DEFAULT_DISK_SIZE_MB,
    }
    if font_file is None:
        manifest["warning"] = "no TTF font found; disk still contains directories but no font file"
    _create_vibeos_fat32_image(disk_img, font_file=font_file, size_mb=DEFAULT_DISK_SIZE_MB)
    _write_json(workdir / "qemu_disk_manifest.json", manifest)
    return disk_img


def _resolve_font_file(value: str) -> Path | None:
    if value:
        path = Path(value).expanduser()
        if path.exists() and path.is_file():
            return path.resolve()
    for candidate in DEFAULT_FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists() and path.is_file():
            return path.resolve()
    return None


def _create_vibeos_fat32_image(image_path: Path, *, font_file: Path | None, size_mb: int = DEFAULT_DISK_SIZE_MB) -> None:
    sector_size = 512
    sectors_per_cluster = 8
    reserved_sectors = 32
    num_fats = 2
    total_sectors = size_mb * 1024 * 1024 // sector_size
    fat_size = 1
    while True:
        data_start = reserved_sectors + num_fats * fat_size
        total_clusters = (total_sectors - data_start) // sectors_per_cluster
        needed_fat_size = ((total_clusters + 2) * 4 + sector_size - 1) // sector_size
        if needed_fat_size == fat_size:
            break
        fat_size = needed_fat_size

    next_cluster = 2

    def alloc_cluster() -> int:
        nonlocal next_cluster
        cluster = next_cluster
        next_cluster += 1
        return cluster

    root_cluster = alloc_cluster()
    fonts_cluster = alloc_cluster()
    roboto_cluster = alloc_cluster()
    home_cluster = alloc_cluster()
    user_cluster = alloc_cluster()
    font_data = font_file.read_bytes() if font_file else b""
    font_clusters: list[int] = []
    if font_data:
        cluster_bytes = sectors_per_cluster * sector_size
        for _ in range((len(font_data) + cluster_bytes - 1) // cluster_bytes):
            font_clusters.append(alloc_cluster())

    if next_cluster >= total_clusters + 2:
        raise ValueError("generated FAT32 image is too small for replay resources")

    image_path.parent.mkdir(parents=True, exist_ok=True)
    with image_path.open("wb") as handle:
        handle.truncate(total_sectors * sector_size)

        def write_sector(sector: int, data: bytes) -> None:
            handle.seek(sector * sector_size)
            handle.write(data.ljust(sector_size, b"\0")[:sector_size])

        def write_cluster(cluster: int, data: bytes) -> None:
            sector = reserved_sectors + num_fats * fat_size + (cluster - 2) * sectors_per_cluster
            cluster_bytes = sectors_per_cluster * sector_size
            handle.seek(sector * sector_size)
            handle.write(data.ljust(cluster_bytes, b"\0")[:cluster_bytes])

        boot = bytearray(sector_size)
        boot[0:3] = b"\xeb\x58\x90"
        boot[3:11] = b"MSWIN4.1"
        boot[11:13] = (sector_size).to_bytes(2, "little")
        boot[13] = sectors_per_cluster
        boot[14:16] = reserved_sectors.to_bytes(2, "little")
        boot[16] = num_fats
        boot[17:19] = (0).to_bytes(2, "little")
        boot[19:21] = (0).to_bytes(2, "little")
        boot[21] = 0xF8
        boot[22:24] = (0).to_bytes(2, "little")
        boot[24:26] = (63).to_bytes(2, "little")
        boot[26:28] = (255).to_bytes(2, "little")
        boot[28:32] = (0).to_bytes(4, "little")
        boot[32:36] = total_sectors.to_bytes(4, "little")
        boot[36:40] = fat_size.to_bytes(4, "little")
        boot[40:42] = (0).to_bytes(2, "little")
        boot[42:44] = (0).to_bytes(2, "little")
        boot[44:48] = root_cluster.to_bytes(4, "little")
        boot[48:50] = (1).to_bytes(2, "little")
        boot[50:52] = (6).to_bytes(2, "little")
        boot[64] = 0x80
        boot[66] = 0x29
        boot[67:71] = (0xB6C0D001).to_bytes(4, "little")
        boot[71:82] = b"VIBEOS     "
        boot[82:90] = b"FAT32   "
        boot[510:512] = b"\x55\xaa"
        write_sector(0, bytes(boot))
        write_sector(6, bytes(boot))

        fsinfo = bytearray(sector_size)
        fsinfo[0:4] = b"RRaA"
        fsinfo[484:488] = b"rrAa"
        fsinfo[488:492] = (0xFFFFFFFF).to_bytes(4, "little")
        fsinfo[492:496] = next_cluster.to_bytes(4, "little")
        fsinfo[510:512] = b"\x55\xaa"
        write_sector(1, bytes(fsinfo))
        write_sector(7, bytes(fsinfo))

        fat_entries = [0] * (total_clusters + 2)
        fat_entries[0] = 0x0FFFFFF8
        fat_entries[1] = 0x0FFFFFFF
        for cluster in (root_cluster, fonts_cluster, roboto_cluster, home_cluster, user_cluster):
            fat_entries[cluster] = 0x0FFFFFFF
        for i, cluster in enumerate(font_clusters):
            fat_entries[cluster] = font_clusters[i + 1] if i + 1 < len(font_clusters) else 0x0FFFFFFF
        fat = bytearray(fat_size * sector_size)
        for idx, value in enumerate(fat_entries):
            offset = idx * 4
            if offset + 4 > len(fat):
                break
            fat[offset:offset + 4] = (value & 0x0FFFFFFF).to_bytes(4, "little")
        for fat_index in range(num_fats):
            handle.seek((reserved_sectors + fat_index * fat_size) * sector_size)
            handle.write(fat)

        root_entries = [
            _fat_short_dir_entry("FONTS      ", 0x10, fonts_cluster, 0),
            _fat_short_dir_entry("HOME       ", 0x10, home_cluster, 0),
        ]
        fonts_entries = [
            _fat_short_dir_entry(".          ", 0x10, fonts_cluster, 0),
            _fat_short_dir_entry("..         ", 0x10, root_cluster, 0),
            _fat_short_dir_entry("ROBOTO     ", 0x10, roboto_cluster, 0),
        ]
        home_entries = [
            _fat_short_dir_entry(".          ", 0x10, home_cluster, 0),
            _fat_short_dir_entry("..         ", 0x10, root_cluster, 0),
            _fat_short_dir_entry("USER       ", 0x10, user_cluster, 0),
        ]
        user_entries = [
            _fat_short_dir_entry(".          ", 0x10, user_cluster, 0),
            _fat_short_dir_entry("..         ", 0x10, home_cluster, 0),
        ]
        roboto_entries = [
            _fat_short_dir_entry(".          ", 0x10, roboto_cluster, 0),
            _fat_short_dir_entry("..         ", 0x10, fonts_cluster, 0),
        ]
        if font_data:
            short_name = "ROBOT~1 TTF"
            roboto_entries.extend(_fat_lfn_entries("Roboto-Regular.ttf", short_name))
            roboto_entries.append(_fat_short_dir_entry(short_name, 0x20, font_clusters[0], len(font_data)))

        write_cluster(root_cluster, b"".join(root_entries))
        write_cluster(fonts_cluster, b"".join(fonts_entries))
        write_cluster(home_cluster, b"".join(home_entries))
        write_cluster(user_cluster, b"".join(user_entries))
        write_cluster(roboto_cluster, b"".join(roboto_entries))
        cluster_bytes = sectors_per_cluster * sector_size
        for i, cluster in enumerate(font_clusters):
            chunk = font_data[i * cluster_bytes:(i + 1) * cluster_bytes]
            write_cluster(cluster, chunk)


def _fat_short_dir_entry(short_name: str, attr: int, cluster: int, size: int) -> bytes:
    raw = short_name.encode("ascii")
    if len(raw) != 11:
        raise ValueError(f"FAT short name must be 11 bytes: {short_name!r}")
    entry = bytearray(32)
    entry[0:11] = raw
    entry[11] = attr
    entry[20:22] = ((cluster >> 16) & 0xFFFF).to_bytes(2, "little")
    entry[26:28] = (cluster & 0xFFFF).to_bytes(2, "little")
    entry[28:32] = size.to_bytes(4, "little")
    return bytes(entry)


def _fat_lfn_entries(long_name: str, short_name: str) -> list[bytes]:
    checksum = _fat_lfn_checksum(short_name.encode("ascii"))
    chars = [ord(ch) for ch in long_name]
    chunks = [chars[i:i + 13] for i in range(0, len(chars), 13)]
    entries: list[bytes] = []
    for disk_index, chunk_index in enumerate(range(len(chunks), 0, -1)):
        chunk = chunks[chunk_index - 1]
        values = chunk[:]
        if chunk_index == len(chunks):
            values.append(0)
        while len(values) < 13:
            values.append(0xFFFF)
        entry = bytearray(32)
        entry[0] = chunk_index | (0x40 if disk_index == 0 else 0)
        entry[11] = 0x0F
        entry[12] = 0
        entry[13] = checksum
        entry[26:28] = b"\0\0"
        positions = [1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30]
        for value, pos in zip(values, positions):
            entry[pos:pos + 2] = value.to_bytes(2, "little")
        entries.append(bytes(entry))
    return entries


def _fat_lfn_checksum(short_name: bytes) -> int:
    checksum = 0
    for byte in short_name:
        checksum = (((checksum & 1) << 7) + (checksum >> 1) + byte) & 0xFF
    return checksum


def _prepare_llm_environment(args: argparse.Namespace) -> None:
    secret_env = Path(getattr(args, "secret_env", "") or "")
    secret_values: dict[str, str] = {}
    if secret_env.exists():
        secret_values = _read_env_file(secret_env)
        for key, value in secret_values.items():
            os.environ[key] = value
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key and not os.environ.get("BMC_AGENT_LLM_API_KEY"):
        os.environ["BMC_AGENT_LLM_API_KEY"] = openrouter_key
    if secret_values.get("OPENROUTER_API_KEY"):
        os.environ["BMC_AGENT_LLM_API_KEY"] = secret_values["OPENROUTER_API_KEY"]
    os.environ.setdefault("BMC_AGENT_LLM_PROVIDER", "openai")
    os.environ.setdefault("BMC_AGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    os.environ.setdefault("BMC_AGENT_LLM_MODEL", getattr(args, "llm_model", "") or "anthropic/claude-sonnet-4.6")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _sanitize_error(text: str) -> str:
    text = re.sub(
        r"https://openrouter\.ai/workspaces/[^\"'\s]+/keys/[A-Za-z0-9_-]+",
        "https://openrouter.ai/workspaces/<redacted>/keys/<redacted>",
        text,
    )
    text = re.sub(r"\bsk-or-v1-[A-Za-z0-9_-]+\b", "sk-or-v1-<redacted>", text)
    text = re.sub(r"\bsk-ant-[A-Za-z0-9_-]+\b", "sk-ant-<redacted>", text)
    return text


def _invalid_confirmed_marker_reason(text: str) -> str | None:
    lowered = text.lower()
    for match in re.finditer(r"dynamic:confirmed", lowered):
        marker_tail = lowered[match.start(): match.start() + 240]
        if any(term in marker_tail for term in ("without fault", "no fault", "not triggered", "not exercised", "safe path")):
            return "DYNAMIC:CONFIRMED cannot describe a no-fault/safe-path observation"
    return None


def _invalid_unquoted_marker_reason(text: str) -> str | None:
    code = _strip_c_comments_and_strings(text)
    if "DYNAMIC:" in code or "VALIDATION:" in code:
        return "verdict markers must be emitted with printf string literals, not raw C tokens"
    return None


def _invalid_generated_call_reason(text: str) -> str | None:
    code = _strip_c_comments_and_strings(text)
    if re.search(r"\bstbtt_[A-Za-z0-9_]*\s*\(", code):
        return "generated replay may not call internal stbtt_* functions directly; use public ttf_* APIs"
    if re.search(r"\bkapi_(?!init\b)[A-Za-z0-9_]*\s*\(", code):
        return "generated replay may not call private kapi_* helper functions directly; use public VFS APIs or kapi.<field>"
    if re.search(r"\bvfs_open\s*\(", code):
        return "generated replay used nonexistent vfs_open(); use vfs_create, vfs_lookup, or vfs_open_handle"
    return None


def _strip_c_comments_and_strings(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//.*", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
    return text


def _qemu_command(qemu_bin: str, kernel_bin: Path | str, disk_img: Path | str | None = None) -> list[str]:
    cmd = [
        qemu_bin,
        "-M", "virt,secure=on",
        "-cpu", "cortex-a72",
        "-m", "512M",
        "-rtc", "base=utc,clock=host",
        "-global", "virtio-mmio.force-legacy=false",
        "-display", "none",
        "-serial", "stdio",
        "-no-reboot",
    ]
    if disk_img:
        cmd.extend([
            "-device", "virtio-blk-device,drive=hd0",
            "-drive", f"file={disk_img},if=none,format=raw,id=hd0",
        ])
    cmd.extend([
        "-device", "virtio-net-device,netdev=net0",
        "-netdev", "user,id=net0",
        "-bios", str(kernel_bin),
    ])
    return cmd


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=_coerce_output(exc.stdout),
            stderr=_coerce_output(exc.stderr) + f"\n[BMC-DYN] command timed out after {timeout}s\n",
        )


def _has_target_fault(output: str) -> bool:
    return any(
        needle in output
        for needle in ("KERNEL PANIC", "Data Abort", "Instruction Abort", "SError", "division by zero", "Undefined Instruction")
    )


def _observed_safety_event(case: str) -> str | None:
    rule = REPLAY_CATALOG.get(case)
    if rule and rule.category in {
        "public_api_pointer_guard",
        "public_api_handle_guard",
        "public_api_string_guard",
    }:
        return rule.target_event
    return None


def _write_text(path: Path, text: str) -> None:
    path.write_text(text or "", encoding="utf-8", errors="replace")


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _first_marker_line(output: str) -> str | None:
    marker = None
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("DYNAMIC:") or stripped.startswith("VALIDATION:"):
            marker = stripped
    return marker


def _record_marker(workdir: Path, marker: str) -> None:
    _write_text(workdir / "validation_marker.txt", marker + "\n")


def _emit_marker(workdir: Path, marker: str) -> None:
    _record_marker(workdir, marker)
    print(marker, flush=True)


def _tail(text: str, *, limit: int = 6000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return "...<truncated>...\n" + text[-limit:]


def _coerce_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _cleanup(path: Path, *, keep: bool) -> None:
    if keep:
        print(f"[BMC-DYN] kept worktree: {path}", flush=True)
        return
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
