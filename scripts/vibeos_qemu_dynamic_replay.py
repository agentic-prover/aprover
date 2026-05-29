#!/usr/bin/env python3
"""VibeOS target replay adapter for BMC-Agent dynamic validation.

This script is intentionally VibeOS-specific. It copies a VibeOS checkout to a
temporary worktree, injects a small replay call into ``kernel/kernel.c``, builds
the QEMU kernel image, optionally boots it under ``qemu-system-aarch64``, and
prints verdict markers consumed by ``DynamicValidator``:

  * ``DYNAMIC:CONFIRMED ...`` when target output shows a kernel panic/fault or
    the injected replay observes a target-side semantic mismatch.
  * ``VALIDATION:PASS`` when the replay reaches its "no crash" marker.

Without QEMU, use ``--build-only`` to validate the patch/build part; that mode
does not emit a pass/fail dynamic marker because no target execution happened.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


CASE_BY_ENTRY = {
    "net_get_mac": "net_get_mac_null",
    "kapi_file_size": "kapi_file_size_invalid_ptr",
    "hal_dma_fb_copy": "hal_dma_fb_copy_overflow",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("BMC_AGENT_VIBEOS_REPO", ""),
        help="Path to a VibeOS checkout. Defaults to BMC_AGENT_VIBEOS_REPO.",
    )
    parser.add_argument(
        "--case",
        default=os.environ.get("BMC_AGENT_VIBEOS_REPLAY_CASE", "auto"),
        choices=[
            "auto",
            "boot_smoke",
            "net_get_mac_null",
            "kapi_file_size_invalid_ptr",
            "hal_dma_fb_copy_overflow",
            "hal_dma_fb_copy_null_dst",
        ],
        help="Replay case. 'auto' maps BMC_AGENT_DYN_QEMU_METADATA entry_function.",
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("BMC_AGENT_DYN_QEMU_WORKDIR")
        or os.environ.get("BMC_AGENT_DYN_TARGET_WORKDIR")
        or "",
        help="Artifact directory supplied by DynamicValidator.",
    )
    parser.add_argument("--build-timeout", type=int, default=180)
    parser.add_argument("--qemu-timeout", type=int, default=20)
    parser.add_argument(
        "--qemu-bin",
        default=os.environ.get("BMC_AGENT_VIBEOS_QEMU_BIN", "qemu-system-aarch64"),
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Patch and build the VibeOS kernel, but do not launch QEMU.",
    )
    parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help="Keep the temporary VibeOS worktree after completion.",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    if repo is None or not repo.exists():
        print(
            "VALIDATION:INCONCLUSIVE missing VibeOS repo; pass --repo or set "
            "BMC_AGENT_VIBEOS_REPO",
            flush=True,
        )
        return 2

    metadata = _load_metadata()
    case = _resolve_case(args.case, metadata)
    if case is None:
        entry = metadata.get("entry_function", "")
        print(f"VALIDATION:INCONCLUSIVE unsupported VibeOS replay entry: {entry}", flush=True)
        return 3

    workdir = Path(args.workdir).resolve() if args.workdir else Path(
        tempfile.mkdtemp(prefix="vibeos-dyn-")
    )
    workdir.mkdir(parents=True, exist_ok=True)
    replay_root = workdir / "vibeos_replay"
    if replay_root.exists():
        shutil.rmtree(replay_root)

    print(f"[BMC-DYN] VibeOS replay case={case}", flush=True)
    print(f"[BMC-DYN] source repo={repo}", flush=True)
    print(f"[BMC-DYN] worktree={replay_root}", flush=True)

    _ensure_tlse_submodule(repo)
    _copy_vibeos_repo(repo, replay_root)
    if case != "boot_smoke":
        _inject_replay(replay_root, case)

    build_result = _run(
        ["make", "TARGET=qemu", "PRINTF=uart"],
        cwd=replay_root,
        timeout=args.build_timeout,
    )
    _write_text(workdir / "vibeos_build.stdout.log", build_result.stdout)
    _write_text(workdir / "vibeos_build.stderr.log", build_result.stderr)
    if build_result.returncode != 0:
        print("[BMC-DYN] VibeOS build failed", flush=True)
        print(_tail(build_result.stdout + "\n" + build_result.stderr), flush=True)
        _cleanup(replay_root, keep=args.keep_worktree)
        return 4

    kernel_bin = replay_root / "build" / "vibeos.bin"
    print(f"[BMC-DYN] VibeOS build OK: {kernel_bin}", flush=True)
    if args.build_only:
        print("[BMC-DYN] BUILD_ONLY_OK target execution not attempted", flush=True)
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0

    qemu_bin = shutil.which(args.qemu_bin)
    if not qemu_bin:
        print(
            f"VALIDATION:INCONCLUSIVE qemu binary not found: {args.qemu_bin}",
            flush=True,
        )
        _cleanup(replay_root, keep=args.keep_worktree)
        return 5

    qemu_cmd = _qemu_command(qemu_bin, kernel_bin)
    qemu_result = _run(qemu_cmd, cwd=replay_root, timeout=args.qemu_timeout)
    combined = qemu_result.stdout + "\n" + qemu_result.stderr
    _write_text(workdir / "vibeos_qemu.stdout.log", qemu_result.stdout)
    _write_text(workdir / "vibeos_qemu.stderr.log", qemu_result.stderr)
    print(_tail(combined, limit=12000), flush=True)

    if "DYNAMIC:CONFIRMED" in combined:
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0
    if _has_target_fault(combined):
        print("DYNAMIC:CONFIRMED signal=TARGET_PANIC", flush=True)
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0
    if case == "boot_smoke" and "[BOOT] Kernel loaded successfully!" in combined:
        print("VALIDATION:PASS boot_smoke reached VibeOS boot marker", flush=True)
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0
    if f"[BMC-DYN] case {case} no crash" in combined:
        print(f"VALIDATION:PASS replay case {case} reached no-crash marker", flush=True)
        _cleanup(replay_root, keep=args.keep_worktree)
        return 0

    print(
        f"VALIDATION:INCONCLUSIVE replay case {case} emitted no target verdict marker",
        flush=True,
    )
    _cleanup(replay_root, keep=args.keep_worktree)
    return 6


def _load_metadata() -> dict:
    path = (
        os.environ.get("BMC_AGENT_DYN_QEMU_METADATA")
        or os.environ.get("BMC_AGENT_DYN_TARGET_METADATA")
        or ""
    )
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


def _ensure_tlse_submodule(repo: Path) -> None:
    if (repo / "vendor" / "tlse" / "tlse.c").exists():
        return
    if not (repo / ".git").exists():
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
    ignore = shutil.ignore_patterns(
        ".git",
        "build",
        "disk.img",
        "*.o",
        "*.elf",
        "*.img",
        "__pycache__",
    )
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
        printf("DYNAMIC:CONFIRMED signal=SEMANTIC_MISMATCH hal_dma_fb_copy overflow truncated copy\\n");
    } else {
        printf("[BMC-DYN] case hal_dma_fb_copy_overflow no mismatch dst4=0x%x\\n", _bmc_dyn_dma_dst[4]);
    }
"""
    elif case == "hal_dma_fb_copy_null_dst":
        anchor = "    hal_dma_init();\n"
        injection = """
    printf("[BMC-DYN] case hal_dma_fb_copy_null_dst start\\n");
    uint32_t _bmc_dyn_src_pixel[1] = {0x12345678u};
    hal_dma_fb_copy((uint32_t *)0, _bmc_dyn_src_pixel, 1, 1);
    printf("[BMC-DYN] case hal_dma_fb_copy_null_dst no crash\\n");
"""
    else:
        raise ValueError(f"unsupported VibeOS replay case: {case}")

    if injection.strip() in text:
        return
    if anchor not in text:
        raise RuntimeError(f"could not find VibeOS injection anchor for case {case}")
    kernel_c.write_text(text.replace(anchor, anchor + injection, 1), encoding="utf-8")


def _qemu_command(qemu_bin: str, kernel_bin: Path) -> list[str]:
    return [
        qemu_bin,
        "-M",
        "virt,secure=on",
        "-cpu",
        "cortex-a72",
        "-m",
        "512M",
        "-rtc",
        "base=utc,clock=host",
        "-global",
        "virtio-mmio.force-legacy=false",
        "-device",
        "virtio-net-device,netdev=net0",
        "-netdev",
        "user,id=net0",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-no-reboot",
        "-bios",
        str(kernel_bin),
    ]


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=_coerce_output(exc.stdout),
            stderr=_coerce_output(exc.stderr) + f"\n[BMC-DYN] command timed out after {timeout}s\n",
        )


def _has_target_fault(output: str) -> bool:
    needles = (
        "KERNEL PANIC",
        "Data Abort",
        "Instruction Abort",
        "SError",
        "division by zero",
        "Undefined Instruction",
    )
    return any(n in output for n in needles)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text or "", encoding="utf-8", errors="replace")


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
