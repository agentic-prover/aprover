from scripts.vibeos_qemu_dynamic_replay import (
    ADMISSION_PROTOCOL_VERSION,
    REPLAY_CATALOG,
    _catalog_manifest,
    _create_vibeos_fat32_image,
    _first_marker_line,
    _guard_replay_injection,
    _has_target_fault,
    _observed_safety_event,
    _sanitize_error,
    _validate_llm_plan,
    _vibeos_build_command,
    _qemu_command,
    _replay_metadata,
    _resolve_case,
)


def test_resolve_case_from_metadata():
    assert _resolve_case("auto", {"entry_function": "net_get_mac"}) == "net_get_mac_null"
    assert _resolve_case("auto", {"entry_function": "kapi_file_size"}) == "kapi_file_size_invalid_ptr"
    assert _resolve_case("auto", {"entry_function": "kapi_delete"}) == "kapi_delete_invalid_path"
    assert _resolve_case("auto", {"entry_function": "kapi_rename"}) == "kapi_rename_invalid_path"
    assert _resolve_case("auto", {"entry_function": "kapi_get_datetime"}) == "kapi_get_datetime_invalid_ptr"
    assert _resolve_case("auto", {"entry_function": "mouse_set_pos"}) == "mouse_set_pos_large_coordinate"
    assert _resolve_case("auto", {"entry_function": "malloc"}) == "malloc_size_wrap"
    assert _resolve_case("auto", {"entry_function": "vfs_read"}) == "vfs_read_null_file_data"
    assert _resolve_case("auto", {"entry_function": "not_supported"}) is None


def test_explicit_case_passthrough():
    assert _resolve_case("boot_smoke", {}) == "boot_smoke"


def test_target_fault_markers():
    assert _has_target_fault("... Data Abort ...")
    assert _has_target_fault("KERNEL PANIC: bad access")
    assert not _has_target_fault("[BOOT] Kernel loaded successfully!")


def test_target_marker_parser_uses_final_verdict_marker():
    marker = _first_marker_line(
        "[BMC-DYN] start\n"
        "DYNAMIC:CONFIRMED target_event=SAFE_PATH no fault\n"
        "DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=REAL_CONCERN later\n"
    )
    assert marker == "DYNAMIC:OBSERVED_SAFETY_CONCERN target_event=REAL_CONCERN later"


def test_observed_safety_event_mapping():
    assert _observed_safety_event("net_get_mac_null") == "UNGUARDED_NULL_POINTER"
    assert _observed_safety_event("kapi_file_size_invalid_ptr") == "UNGUARDED_INVALID_POINTER"
    assert _observed_safety_event("kapi_delete_invalid_path") == "UNGUARDED_INVALID_STRING_POINTER"
    assert _observed_safety_event("hal_dma_fb_copy_overflow") is None
    assert _observed_safety_event("mouse_set_pos_large_coordinate") is None


def test_replay_catalog_records_selection_rule():
    rule = REPLAY_CATALOG["net_get_mac_null"]
    assert rule.category == "public_api_pointer_guard"
    assert "BUG-" not in rule.selection_rule


def test_replay_metadata_preserves_rule_and_bmc_metadata():
    metadata = {
        "entry_function": "net_get_mac",
        "failing_property": "net_get_mac.precondition_instance.3",
    }
    payload = _replay_metadata("net_get_mac_null", REPLAY_CATALOG["net_get_mac_null"], metadata)
    assert payload["metadata_entry_function"] == "net_get_mac"
    assert payload["metadata_failing_property"] == "net_get_mac.precondition_instance.3"
    assert payload["admission_protocol_version"] == ADMISSION_PROTOCOL_VERSION
    assert payload["replay_rule"]["category"] == "public_api_pointer_guard"


def test_catalog_manifest_is_rule_oriented():
    manifest = _catalog_manifest()
    assert {item["case"] for item in manifest} == set(REPLAY_CATALOG)
    assert all(item["admission_protocol_version"] == ADMISSION_PROTOCOL_VERSION for item in manifest)
    assert all("selection_rule" in item for item in manifest)


def test_qemu_command_contains_kernel_image():
    cmd = _qemu_command("qemu-system-aarch64", "/tmp/vibeos.bin")
    assert cmd[0] == "qemu-system-aarch64"
    assert "-bios" in cmd
    assert "/tmp/vibeos.bin" in [str(part) for part in cmd]


def test_qemu_command_attaches_optional_disk_image():
    cmd = _qemu_command("qemu-system-aarch64", "/tmp/vibeos.bin", "/tmp/disk.img")
    assert "-device" in cmd
    assert "virtio-blk-device,drive=hd0" in cmd
    assert "file=/tmp/disk.img,if=none,format=raw,id=hd0" in cmd


def test_build_command_enables_bmc_dyn_replay():
    cmd = _vibeos_build_command(enable_replay=True)
    assert "TARGET=qemu" in cmd
    assert any("BMC_DYN_REPLAY" in part for part in cmd)
    assert not any("BMC_DYN_REPLAY" in part for part in _vibeos_build_command(enable_replay=False))


def test_guard_replay_injection_is_compile_time_opt_in():
    guarded = _guard_replay_injection('printf("[BMC-DYN] demo\\n");')
    assert "#ifdef BMC_DYN_REPLAY" in guarded
    assert "#endif" in guarded


def test_create_vibeos_fat32_image_contains_font_tree(tmp_path):
    font = tmp_path / "font.ttf"
    font.write_bytes(b"fake-ttf")
    image = tmp_path / "disk.img"

    _create_vibeos_fat32_image(image, font_file=font, size_mb=8)

    data = image.read_bytes()
    assert data[510:512] == b"\x55\xaa"
    assert data[82:90] == b"FAT32   "
    assert b"FONTS" in data
    assert b"ROBOTO" in data
    assert b"HOME" in data
    assert b"USER" in data
    assert b"ROBOT~1 TTF" in data
    assert b"fake-ttf" in data


def test_adapter_sanitizes_llm_provider_errors():
    raw = (
        "Key limit exceeded at "
        "https://openrouter.ai/workspaces/default/keys/abcdef123456 "
        "for sk-or-v1-secret"
    )
    sanitized = _sanitize_error(raw)
    assert "abcdef123456" not in sanitized
    assert "sk-or-v1-secret" not in sanitized
    assert "<redacted>" in sanitized


def test_adapter_rejects_unsafe_generated_plan():
    ok, error = _validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": 'printf("[BMC-DYN] case demo start\\n"); fork();',
        }
    )
    assert not ok
    assert "banned token" in (error or "")


def test_adapter_rejects_generated_extern_state_access():
    ok, error = _validate_llm_plan(
        {
            "supported": True,
            "patch_file": "kernel/kernel.c",
            "anchor": "after_kapi_init_log",
            "c_injection": 'printf("[BMC-DYN] case demo start\\n"); extern int hidden_state;',
        }
    )
    assert not ok
    assert "banned token" in (error or "")


def test_adapter_rejects_confirmed_no_fault_marker():
    ok, error = _validate_llm_plan(
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
