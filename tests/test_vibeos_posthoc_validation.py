from experiments.vibeos_qemu_validation import posthoc_validate_findings as posthoc


class Args:
    with_fat32_disk = True


def test_sanitize_error_redacts_provider_key_material():
    raw = (
        "HTTP 403 Manage it using "
        "https://openrouter.ai/workspaces/default/keys/abcdef123456 and "
        "sk-or-v1-secret"
    )
    sanitized = posthoc.sanitize_error(raw)
    assert "abcdef123456" not in sanitized
    assert "sk-or-v1-secret" not in sanitized
    assert "<redacted>" in sanitized


def test_validate_llm_plan_accepts_bounded_kernel_injection():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "target_event": "NON_HEAP_FREE",
            "expected_outcome": "observed_safety_concern",
            "c_injection": 'printf("[BMC-DYN] case demo start\\n");\n'
            'printf("DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=NON_HEAP_FREE demo\\n");',
        }
    )
    assert ok
    assert error is None


def test_validate_llm_plan_rejects_shell_or_unbounded_injection():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": 'printf("[BMC-DYN] case demo start\\n"); system("sh");',
        }
    )
    assert not ok
    assert "banned token" in (error or "")


def test_validate_llm_plan_rejects_extern_state_access():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": 'printf("[BMC-DYN] case demo start\\n"); extern int hidden_state;',
        }
    )
    assert not ok
    assert "banned token" in (error or "")


def test_validate_llm_plan_rejects_confirmed_no_fault_marker():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": (
                'printf("[BMC-DYN] case demo start\\n");\n'
                'printf("DYNAMIC:CONFIRMED target_event=SAFE_PATH returned without fault\\n");'
            ),
        }
    )
    assert not ok
    assert "no-fault" in (error or "")


def test_validate_llm_plan_rejects_raw_unquoted_verdict_marker():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": (
                'printf("[BMC-DYN] case demo start\\n");\n'
                "VALIDATION:INCONCLUSIVE target_event=NO_TARGET_VERDICT bad raw token"
            ),
        }
    )
    assert not ok
    assert "printf" in (error or "")


def test_validate_llm_plan_does_not_reject_prior_safe_diagnostic_on_same_source_line():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": (
                'printf("[BMC-DYN] safe path diagnostic\\n"); '
                'printf("DYNAMIC:CONFIRMED target_event=BAD_RETURN returned 999\\n");'
            ),
        }
    )
    assert ok
    assert error is None


def test_validate_llm_plan_rejects_internal_stbtt_direct_call():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": (
                'printf("[BMC-DYN] case demo start\\n");\n'
                "stbtt_GetCodepointHMetrics(0, 0, 0, 0);"
            ),
        }
    )
    assert not ok
    assert "stbtt" in (error or "")


def test_validate_llm_plan_rejects_private_kapi_helper_call():
    ok, error = posthoc.validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": 'printf("[BMC-DYN] case demo start\\n"); kapi_write(0, 0, 0);',
        }
    )
    assert not ok
    assert "kapi" in (error or "")


def test_apply_generated_kernel_patch_only_uses_allowed_anchor(tmp_path):
    replay_root = tmp_path / "vibeos"
    kernel_dir = replay_root / "kernel"
    kernel_dir.mkdir(parents=True)
    kernel_c = kernel_dir / "kernel.c"
    kernel_c.write_text(
        "void kernel_main(void) {\n"
        '    printf("[KERNEL] Kernel API initialized\\n");\n'
        "    process_init();\n"
        "}\n",
        encoding="utf-8",
    )

    result = posthoc.apply_generated_kernel_patch(
        replay_root,
        {
            "anchor": "after_kapi_init_log",
            "c_injection": 'printf("[BMC-DYN] case generated start\\n");',
        },
    )

    assert result["ok"]
    assert "[BMC-DYN] case generated start" in kernel_c.read_text(encoding="utf-8")
    assert "kernel/kernel.c.before" in result["diff"]


def test_summarize_separates_catalog_llm_and_still_unsupported_rows():
    summary = posthoc.summarize(
        [
            {"supported": True, "llm_generated": False, "outcome": "qemu_confirmed"},
            {"supported": False, "llm_generated": True, "outcome": "observed_safety_concern"},
            {
                "supported": False,
                "llm_generated": False,
                "outcome": "unsupported_by_current_replay_catalog",
            },
        ]
    )

    assert summary["catalog_supported_rows"] == 1
    assert summary["llm_generated_rows"] == 1
    assert summary["validation_attempted_rows"] == 2
    assert summary["unsupported_rows"] == 1


def test_read_marker_uses_final_stdout_verdict_when_no_marker_file(tmp_path):
    marker = posthoc.read_marker(
        tmp_path,
        "DYNAMIC:CONFIRMED target_event=SAFE_PATH no fault\n"
        "DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=REAL_CONCERN later\n",
    )
    assert marker == "DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=REAL_CONCERN later"


def test_classify_inconclusive_marker():
    assert (
        posthoc.classify_marker(
            "VALIDATION:INCONCLUSIVE target_event=NO_TARGET_VERDICT generated replay emitted diagnostics but no verdict marker",
            124,
        )
        == "inconclusive"
    )


def test_render_summary_includes_concrete_unsupported_reason():
    summary = posthoc.summarize(
        [
            {
                "finding_set": "set",
                "bug_id": "BUG-1",
                "function": "foo",
                "supported": False,
                "llm_generated": True,
                "outcome": "llm_marked_unsupported",
                "llm_plan_error": "QEMU stub returns -1, so target path is absent.",
                "artifact_dir": "/tmp/a",
            }
        ]
    )
    rendered = posthoc.render_summary_md(
        summary,
        [
            {
                "finding_set": "set",
                "bug_id": "BUG-1",
                "function": "foo",
                "supported": False,
                "llm_generated": True,
                "outcome": "llm_marked_unsupported",
                "llm_plan_error": "QEMU stub returns -1, so target path is absent.",
                "artifact_dir": "/tmp/a",
            }
        ],
    )
    assert "Unsupported And Inconclusive Details" in rendered
    assert "QEMU stub returns -1" in rendered


def test_extract_source_context_includes_sibling_header(tmp_path):
    kernel = tmp_path / "kernel"
    kernel.mkdir()
    (kernel / "vfs.h").write_text(
        "vfs_node_t *vfs_create(const char *path);\n"
        "int vfs_append(vfs_node_t *file, const char *buf, size_t size);\n",
        encoding="utf-8",
    )
    (kernel / "vfs.c").write_text(
        '#include "vfs.h"\n'
        "int vfs_append(vfs_node_t *file, const char *buf, size_t size) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )

    context = posthoc.extract_source_context(tmp_path, "kernel/vfs.c", "vfs_append")

    assert "Header: kernel/vfs.h" in context
    assert "vfs_create" in context
    assert "vfs_append" in context


def test_summarize_build_failure_extracts_actionable_errors():
    reason = posthoc.summarize_build_failure(
        "kernel/kernel.c:278:21: warning: implicit declaration of function 'vfs_open'\n"
        "aarch64-linux-gnu-ld: undefined reference to `vfs_open'\n"
        "make: *** [Makefile:157: build/vibeos.elf] Error 1\n",
        "",
    )

    assert reason.startswith("generated build failed:")
    assert "vfs_open" in reason
    assert "undefined reference" in reason


def test_llm_prompt_mentions_qemu_disk_when_enabled(tmp_path):
    finding = tmp_path / "BUG-1.md"
    finding.write_text("# BUG-1 in `ttf_init`\n", encoding="utf-8")
    kernel = tmp_path / "kernel"
    kernel.mkdir()
    (kernel / "ttf.c").write_text("int ttf_init(void) { return 0; }\n", encoding="utf-8")
    row = {
        "finding_path": str(finding),
        "module": "kernel/ttf.c",
        "function": "ttf_init",
    }

    prompt = posthoc.build_llm_replay_prompt(row, tmp_path, Args())

    assert "generated FAT32 virtio-blk disk" in prompt
    assert "/fonts/Roboto/Roboto-Regular.ttf" in prompt
    assert "public API path" in prompt
