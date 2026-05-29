from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vibeos_qemu_dynamic_replay.py"


def _make_fake_vibeos_repo(root: Path) -> None:
    (root / "kernel").mkdir(parents=True)
    (root / "vendor" / "tlse").mkdir(parents=True)
    (root / "vendor" / "tlse" / "tlse.c").write_text("/* fake */\n", encoding="utf-8")
    (root / "kernel" / "kernel.c").write_text(
        """
#include <stdint.h>
void kernel_main(void) {
    hal_dma_init();
    net_init();
    kapi_init();
    printf("[KERNEL] Kernel API initialized\\n");
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "Makefile").write_text(
        """
all:
\tmkdir -p build
\tprintf 'fake kernel' > build/vibeos.bin
""".lstrip(),
        encoding="utf-8",
    )


def test_vibeos_replay_injects_case_and_builds(tmp_path):
    repo = tmp_path / "vibeos"
    _make_fake_vibeos_repo(repo)
    work = tmp_path / "work"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--case",
            "net_get_mac_null",
            "--workdir",
            str(work),
            "--build-only",
            "--keep-worktree",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BUILD_ONLY_OK" in proc.stdout
    patched = (work / "vibeos_replay" / "kernel" / "kernel.c").read_text(encoding="utf-8")
    assert "net_get_mac((uint8_t *)0);" in patched


def test_vibeos_replay_maps_qemu_panic_to_dynamic_confirmed(tmp_path):
    repo = tmp_path / "vibeos"
    _make_fake_vibeos_repo(repo)
    work = tmp_path / "work"
    wrapper = tmp_path / "fake-qemu"
    wrapper.write_text(
        "#!/bin/sh\nprintf 'VIBE\\nKERNEL PANIC: Data Abort\\n'\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--case",
            "kapi_file_size_invalid_ptr",
            "--workdir",
            str(work),
            "--qemu-bin",
            str(wrapper),
            "--qemu-timeout",
            "5",
            "--keep-worktree",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DYNAMIC:CONFIRMED signal=TARGET_PANIC" in proc.stdout
    patched = (work / "vibeos_replay" / "kernel" / "kernel.c").read_text(encoding="utf-8")
    assert "kapi.file_size((void *)0xffffffffffffffffULL)" in patched


def test_vibeos_replay_injects_hal_dma_overflow_semantic_check(tmp_path):
    repo = tmp_path / "vibeos"
    _make_fake_vibeos_repo(repo)
    work = tmp_path / "work"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--case",
            "hal_dma_fb_copy_overflow",
            "--workdir",
            str(work),
            "--build-only",
            "--keep-worktree",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    patched = (work / "vibeos_replay" / "kernel" / "kernel.c").read_text(encoding="utf-8")
    assert "hal_dma_fb_copy(_bmc_dyn_dma_dst, _bmc_dyn_dma_src, 0x40000001u, 4u)" in patched
    assert "DYNAMIC:CONFIRMED signal=SEMANTIC_MISMATCH" in patched
