#!/usr/bin/env python3
"""Kernel-aware driver wrapper for BMC-Agent experiments.

This wrapper is intentionally outside the main BMC-Agent CLI. It adapts a
prepared Linux-driver slice package to a fairer whole-driver experiment:

* discover entrypoints automatically from the driver source context;
* ignore case-specific ``bmc_agent_entry_*`` wrappers by default;
* restrict BMC-Agent's parsed function scope to driver-local functions;
* leave kernel functions as external environment symbols instead of
  spec-generating and CBMC-checking them as ordinary local functions.

The verifier still consumes the package's full ``slice/*.i`` translation unit.
Only the analysis/spec function scope is restricted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


WRAPPER_PREFIX = "bmc_agent_entry_"

CALLBACK_FIELDS = {
    # Driver core / buses.
    "probe",
    "remove",
    "disconnect",
    "shutdown",
    "suspend",
    "resume",
    "freeze",
    "thaw",
    "restore",
    # File / char-device operations.
    "open",
    "release",
    "read",
    "write",
    "unlocked_ioctl",
    "compat_ioctl",
    "mmap",
    "poll",
    "llseek",
    # SPI/I2C/net/block-ish operation callbacks.
    "transfer",
    "transfer_one",
    "transfer_one_message",
    "prepare_message",
    "unprepare_message",
    "setup",
    "cleanup",
    "master_xfer",
    "functionality",
    "ndo_open",
    "ndo_stop",
    "ndo_start_xmit",
    "ndo_do_ioctl",
    # PM ops.
    "runtime_suspend",
    "runtime_resume",
    "runtime_idle",
}

STRONG_CALLBACK_FIELDS = {
    "probe",
    "remove",
    "disconnect",
    "shutdown",
    "suspend",
    "resume",
    "freeze",
    "thaw",
    "restore",
    "runtime_suspend",
    "runtime_resume",
    "runtime_idle",
}

GENERATED_SHIM_FUNCTIONS = {
    "__intf_to_usbdev",
    "__intf_to_usbdev_const",
    "devm_kzalloc",
}

ENTRY_SUFFIXES = (
    "_probe",
    "_remove",
    "_disconnect",
    "_shutdown",
    "_suspend",
    "_resume",
    "_init",
    "_exit",
)


@dataclass
class EntryPoint:
    name: str
    kind: str
    evidence: str
    score: int


@dataclass
class PackageInfo:
    package: Path
    source_i: Path
    analysis_source: Path
    provenance: dict[str, Any]
    context_packet: dict[str, Any] | None = None


@dataclass
class Discovery:
    package: str
    source_i: str
    analysis_source: str
    driver_functions: list[str]
    entrypoints: list[EntryPoint]
    driver_closure: list[str]
    external_symbols: list[str]
    context_packet_summary: dict[str, Any] | None = None
    ignored_wrappers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "source_i": self.source_i,
            "analysis_source": self.analysis_source,
            "driver_functions": self.driver_functions,
            "entrypoints": [entry.__dict__ for entry in self.entrypoints],
            "driver_closure": self.driver_closure,
            "external_symbols": self.external_symbols,
            "context_packet_summary": self.context_packet_summary,
            "ignored_wrappers": self.ignored_wrappers,
            "warnings": self.warnings,
        }


@dataclass
class GeneratedHarness:
    name: str
    kind: str
    entrypoints: list[str]
    source: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "entrypoints": self.entrypoints,
            "source": self.source,
            "note": self.note,
        }


def load_package(package: Path) -> PackageInfo:
    package = package.resolve()
    if not package.exists():
        raise SystemExit(f"package not found: {package}")
    i_files = sorted((package / "slice").glob("*.i"))
    if not i_files:
        raise SystemExit(f"no slice/*.i found under {package}")
    analysis = package / "analysis" / "analysis_context.c"
    if not analysis.exists():
        raise SystemExit(f"analysis context not found: {analysis}")
    provenance_path = package / "provenance.json"
    provenance = {}
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    context_packet_path = package / "analysis" / "context_packet.json"
    context_packet = None
    if context_packet_path.exists():
        context_packet = json.loads(context_packet_path.read_text(encoding="utf-8"))
    return PackageInfo(
        package=package,
        source_i=i_files[0].resolve(),
        analysis_source=analysis.resolve(),
        provenance=provenance,
        context_packet=context_packet,
    )


def parse_driver_context(analysis_source: Path):
    from bmc_agent.source_parser import parse_source_file

    text = analysis_source.read_text(encoding="utf-8", errors="replace")
    parsed = parse_source_file(analysis_source, source_text=text)
    return parsed, text


def is_driver_function(name: str) -> bool:
    return bool(name) and not name.startswith(WRAPPER_PREFIX)


def callback_entries(text: str, driver_functions: set[str]) -> list[EntryPoint]:
    entries: list[EntryPoint] = []
    seen: set[tuple[str, str]] = set()

    def add_entry(field_name: str, fn_name: str, start: int) -> None:
        if field_name not in CALLBACK_FIELDS:
            return
        if fn_name not in driver_functions:
            return
        key = (field_name, fn_name)
        if key in seen:
            return
        seen.add(key)
        score = 100 if field_name in STRONG_CALLBACK_FIELDS else 55
        entries.append(
            EntryPoint(
                name=fn_name,
                kind=f"callback:{field_name}",
                evidence=snippet_at(text, start),
                score=score,
            )
        )

    # Handles common designated initializers: .probe = foo,
    rx = re.compile(r"\.(?P<field>[A-Za-z_]\w*)\s*=\s*(?P<fn>[A-Za-z_]\w*)\s*[,}]")
    for match in rx.finditer(text):
        add_entry(match.group("field"), match.group("fn"), match.start())
    # Handles macro-expanded PM initializers:
    #   .suspend = ((1) ? ((drv_suspend)) : ((void *)0)),
    rx_wrapped = re.compile(
        r"\.(?P<field>[A-Za-z_]\w*)\s*=\s*[^,{};]{0,160}?\b(?P<fn>[A-Za-z_]\w*)\s*\)\)"
    )
    for match in rx_wrapped.finditer(text):
        add_entry(match.group("field"), match.group("fn"), match.start())
    return entries


def module_entries(text: str, driver_functions: set[str]) -> list[EntryPoint]:
    entries: list[EntryPoint] = []
    patterns = [
        r"\bmodule_init\s*\(\s*(?P<fn>[A-Za-z_]\w*)\s*\)",
        r"\bmodule_exit\s*\(\s*(?P<fn>[A-Za-z_]\w*)\s*\)",
        r"\b(?:device|subsys|fs|late|arch)_initcall\s*\(\s*(?P<fn>[A-Za-z_]\w*)\s*\)",
    ]
    for pat in patterns:
        for match in re.finditer(pat, text):
            fn_name = match.group("fn")
            if fn_name in driver_functions:
                entries.append(
                    EntryPoint(
                        name=fn_name,
                        kind="module_lifecycle",
                        evidence=snippet_at(text, match.start()),
                        score=90,
                    )
                )
    return entries


def exported_entries(text: str, parsed, driver_functions: set[str]) -> list[EntryPoint]:
    entries: list[EntryPoint] = []
    rx = re.compile(r"\bEXPORT_SYMBOL(?:_GPL)?\s*\(\s*(?P<fn>[A-Za-z_]\w*)\s*\)")
    for match in rx.finditer(text):
        fn_name = match.group("fn")
        if fn_name in driver_functions and fn_name not in GENERATED_SHIM_FUNCTIONS:
            entries.append(
                EntryPoint(
                    name=fn_name,
                    kind="exported_symbol",
                    evidence=snippet_at(text, match.start()),
                    score=80,
                )
            )

    # Preprocessed EXPORT_SYMBOL macros may disappear in analysis_context.c.
    # As a fallback for driver-library files, include non-static functions
    # when no callback table exists.
    for name, sig in getattr(parsed, "functions", {}).items():
        if (
            name in driver_functions
            and name not in GENERATED_SHIM_FUNCTIONS
            and not getattr(sig, "is_static", False)
        ):
            entries.append(
                EntryPoint(
                    name=name,
                    kind="public_function",
                    evidence="non-static function in driver-local analysis context",
                    score=60,
                )
            )
    return entries


def suffix_entries(driver_functions: set[str]) -> list[EntryPoint]:
    entries: list[EntryPoint] = []
    for name in sorted(driver_functions):
        if any(name.endswith(suffix) for suffix in ENTRY_SUFFIXES):
            entries.append(
                EntryPoint(
                    name=name,
                    kind="name_suffix_fallback",
                    evidence=f"name matches one of {ENTRY_SUFFIXES}",
                    score=30,
                )
            )
    return entries


def context_packet_entries(packet: dict[str, Any] | None, driver_functions: set[str]) -> list[EntryPoint]:
    """Use deterministic preprocessor entrypoint hints when callback parsing loses macros."""
    if not packet:
        return []
    entries: list[EntryPoint] = []
    for raw in packet.get("entrypoints") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        if name not in driver_functions or name in GENERATED_SHIM_FUNCTIONS:
            continue
        kind = str(raw.get("kind") or "context_packet_entrypoint")
        if "callback:" in kind:
            field = kind.split("callback:", 1)[1].split(",", 1)[0]
            score = 100 if field in STRONG_CALLBACK_FIELDS else 55
        elif kind == "public_api_touching_function":
            score = 70
        else:
            score = 60
        evidence = str(raw.get("evidence") or "entrypoint from analysis/context_packet.json")
        entries.append(EntryPoint(name=name, kind=kind, evidence=evidence, score=score))
    return entries


def snippet_at(text: str, pos: int, radius: int = 160) -> str:
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    if len(snippet) > 320:
        snippet = snippet[:300].rstrip() + "..."
    return snippet


def dedupe_entries(entries: Iterable[EntryPoint]) -> list[EntryPoint]:
    best: dict[str, EntryPoint] = {}
    for entry in entries:
        prev = best.get(entry.name)
        if prev is None or entry.score > prev.score:
            best[entry.name] = entry
        elif prev is not None and entry.score == prev.score and entry.kind not in prev.kind:
            prev.kind = f"{prev.kind},{entry.kind}"
    return sorted(best.values(), key=lambda e: (-e.score, e.name))


def entrypoint_bucket(entry: EntryPoint) -> str:
    kind = entry.kind
    name = entry.name
    if "callback:probe" in kind:
        return "probe"
    if "callback:remove" in kind or "callback:disconnect" in kind or "callback:shutdown" in kind:
        return "teardown"
    if "callback:suspend" in kind or "callback:resume" in kind or "callback:runtime_" in kind:
        return "pm"
    if "public_api_touching_function" in kind and (
        name.endswith("_suspend") or name.endswith("_resume") or "_runtime_" in name
    ):
        return "pm"
    if kind == "module_lifecycle":
        return "module"
    if name.endswith("_init") or name.endswith("_probe"):
        return "public_init"
    if name.endswith("_remove") or name.endswith("_disconnect") or name.endswith("_exit"):
        return "public_teardown"
    if "public_function" in kind or "exported_symbol" in kind:
        return "public"
    return "other"


def select_entrypoints(
    entries: list[EntryPoint],
    *,
    policy: str,
    max_entrypoints: int | None,
) -> list[EntryPoint]:
    if policy == "all":
        selected = list(entries)
    elif policy == "ranked":
        selected = list(entries)
    elif policy == "lifecycle":
        # Pick across lifecycle buckets before taking more from the same class.
        # This keeps a small budget from collapsing to "probe only".
        bucket_order = [
            "probe",
            "teardown",
            "pm",
            "module",
            "public_init",
            "public_teardown",
            "public",
            "other",
        ]
        buckets: dict[str, list[EntryPoint]] = {name: [] for name in bucket_order}
        for entry in entries:
            buckets.setdefault(entrypoint_bucket(entry), []).append(entry)
        selected = []
        while True:
            added = False
            for bucket in bucket_order:
                if buckets.get(bucket):
                    selected.append(buckets[bucket].pop(0))
                    added = True
                    if max_entrypoints is not None and len(selected) >= max_entrypoints:
                        return selected
            if not added:
                break
    else:
        raise ValueError(f"unknown entrypoint policy: {policy}")
    if max_entrypoints is not None:
        selected = selected[: max(0, max_entrypoints)]
    return selected


def driver_closure(entrypoints: Iterable[str], call_graph: dict[str, set[str]], driver_functions: set[str]) -> set[str]:
    closure = {name for name in entrypoints if name in driver_functions}
    stack = list(closure)
    while stack:
        name = stack.pop()
        for callee in call_graph.get(name, set()):
            if callee in driver_functions and callee not in closure:
                closure.add(callee)
                stack.append(callee)
    return closure


def external_symbols_for(closure: Iterable[str], call_graph: dict[str, set[str]], driver_functions: set[str]) -> set[str]:
    external: set[str] = set()
    for name in closure:
        for callee in call_graph.get(name, set()):
            if callee not in driver_functions:
                external.add(callee)
    return external


def callback_name_for(entrypoints: Iterable[EntryPoint], callback: str) -> str | None:
    for entry in entrypoints:
        if f"callback:{callback}" in entry.kind:
            return entry.name
    return None


def devm_action_callback_names(text: str, driver_functions: set[str]) -> set[str]:
    """Return driver-local devm action callbacks that must stay inline.

    ``devm_add_action_or_reset()`` registers a cleanup callback.  Generated
    probe-error and teardown harnesses model driver-core devres cleanup by
    calling that callback, so replacing it with a placeholder would erase the
    cleanup behavior the fixed driver relies on.
    """
    callbacks: set[str] = set()
    pattern = re.compile(
        r"\b(?:__)?devm_add_action_or_reset\s*\("
        r"[^,]+,\s*([A-Za-z_][A-Za-z0-9_]*)\s*,",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        name = match.group(1)
        if name in driver_functions:
            callbacks.add(name)
    return callbacks


def devm_regulator_action_callback_names(text: str, driver_functions: set[str]) -> set[str]:
    """Return devm action callbacks whose bodies release a regulator."""
    callbacks = devm_action_callback_names(text, driver_functions)
    regulator_callbacks: set[str] = set()
    for name in callbacks:
        pattern = re.compile(
            rf"\b{re.escape(name)}\s*\([^;{{}}]*\)\s*\{{(?P<body>.*?)\n\}}",
            re.DOTALL,
        )
        match = pattern.search(text)
        if match and re.search(r"\bregulator_disable\s*\(", match.group("body")):
            regulator_callbacks.add(name)
    return regulator_callbacks


def pointer_base_type(param_type: str) -> str | None:
    """Return the concrete pointee type for simple callback pointer params."""
    typ = re.sub(r"\bconst\b|\bvolatile\b|\b__user\b|\b__iomem\b", "", param_type)
    typ = re.sub(r"\s+", " ", typ).strip()
    if "*" not in typ:
        return None
    base = typ.split("*", 1)[0].strip()
    if not base or base == "void":
        return None
    return base


def framework_object_setup_lines(base: str, var_name: str) -> list[str]:
    """Initialize common bus objects enough for generic lifecycle harnesses."""
    if base == "struct platform_device":
        return [
            f"  {var_name}.dev.of_node = (struct device_node *)bmc_agent_valid_alloc(64);",
        ]
    return []


def generated_error_cleanup_harnesses(discovery: Discovery, parsed) -> list[GeneratedHarness]:
    """Generate resource-balance harnesses for failed PM callback returns.

    Successful resume paths may intentionally leave hardware enabled. Error
    returns are different: after a callback reports failure, local resources
    acquired on that path should already be unwound before control returns to
    the framework.
    """
    harnesses: list[GeneratedHarness] = []
    for entry in discovery.entrypoints:
        if not (
            "callback:resume" in entry.kind
            or "callback:runtime_resume" in entry.kind
            or entry.name.endswith("_resume")
        ):
            continue
        sig = getattr(parsed, "functions", {}).get(entry.name)
        if not sig or len(sig.parameters) != 1:
            continue
        if "int" not in re.sub(r"\s+", " ", sig.return_type).split():
            continue
        base = pointer_base_type(sig.parameters[0][0])
        if not base:
            continue
        name = f"bmc_agent_error_cleanup_{entry.name}"
        source = "\n".join(
            [
                "/* Generated by kernel_driver_mode.py.",
                " * Framework callback error-path harness: failed resume must",
                " * leave local clock enables balanced before returning.",
                " */",
                f"int {name}(void)",
                "{",
                f"  {base} __bmc_dev = {{0}};",
                "  bmc_agent_clk_reset_contract();",
                "  bmc_agent_regulator_reset_contract();",
                "  bmc_agent_pm_runtime_reset_contract();",
                f"  int __bmc_ret = {entry.name}(&__bmc_dev);",
                "  __CPROVER_assume(__bmc_ret != 0);",
                "  bmc_agent_clk_lifecycle_balanced();",
                "  bmc_agent_regulator_lifecycle_balanced();",
                "  bmc_agent_pm_runtime_lifecycle_balanced();",
                "  return 0;",
                "}",
                "",
            ]
        )
        harnesses.append(
            GeneratedHarness(
                name=name,
                kind="resume_error_cleanup",
                entrypoints=[entry.name],
                source=source,
                note="Generated from callback table; checks failed resume cleanup only.",
            )
        )
    return harnesses


def generated_probe_error_cleanup_harness(discovery: Discovery, parsed) -> GeneratedHarness | None:
    """Generate a generic one-argument probe-error cleanup harness.

    This is intentionally framework-shaped rather than bug-specific: many
    platform-style probe callbacks acquire runtime-PM references before a later
    registration step can fail. A failed probe must unwind the probe-owned PM
    reference before returning an error to the driver core.
    """
    probe = callback_name_for(discovery.entrypoints, "probe")
    if not probe:
        return None
    probe_sig = getattr(parsed, "functions", {}).get(probe)
    if not probe_sig or len(probe_sig.parameters) != 1:
        return None
    if "int" not in re.sub(r"\s+", " ", probe_sig.return_type).split():
        return None
    probe_base = pointer_base_type(probe_sig.parameters[0][0])
    if not probe_base:
        return None

    name = f"bmc_agent_error_cleanup_{probe}"
    source = "\n".join(
        [
            "/* Generated by kernel_driver_mode.py.",
            " * Generic probe error-path harness: failed probe must leave",
            " * runtime-PM usage count and local lifecycle resources balanced.",
            " */",
            f"int {name}(void)",
            "{",
            f"  {probe_base} __bmc_dev = {{0}};",
            *framework_object_setup_lines(probe_base, "__bmc_dev"),
            "  bmc_agent_i2c_reset_registration_contract();",
            "  bmc_agent_clk_reset_contract();",
            "  bmc_agent_regulator_reset_contract();",
            "  bmc_agent_netdev_reset_contract();",
            "  bmc_agent_pm_runtime_reset_contract();",
            f"  int __bmc_ret = {probe}(&__bmc_dev);",
            "  __CPROVER_assume(__bmc_ret != 0);",
            "  bmc_agent_devm_actions_teardown();",
            "  bmc_agent_clk_lifecycle_balanced();",
            "  bmc_agent_regulator_lifecycle_balanced();",
            "  bmc_agent_pm_runtime_lifecycle_balanced();",
            "  return 0;",
            "}",
            "",
        ]
    )
    return GeneratedHarness(
        name=name,
        kind="probe_error_cleanup",
        entrypoints=[probe],
        source=source,
        note="Generated from one-argument probe callback; checks failed-probe cleanup.",
    )


def generated_usb_probe_error_cleanup_harness(discovery: Discovery, parsed) -> GeneratedHarness | None:
    """Generate a USB probe-error harness for publish-then-unwind bugs.

    A USB probe can publish an object through usb_register_dev() and then fail
    later.  If userspace opened the device in that window, direct freeing on the
    probe error path races the eventual release callback.  The contract itself
    lives in bmc_agent_kfree(); this harness only exposes failed probe paths.
    """
    probe = callback_name_for(discovery.entrypoints, "probe")
    if not probe:
        return None
    probe_sig = getattr(parsed, "functions", {}).get(probe)
    if not probe_sig or len(probe_sig.parameters) != 2:
        return None
    if "int" not in re.sub(r"\s+", " ", probe_sig.return_type).split():
        return None
    probe_if_base = pointer_base_type(probe_sig.parameters[0][0])
    if probe_if_base != "struct usb_interface":
        return None
    if not _is_usb_device_id_pointer(probe_sig.parameters[1][0]):
        return None

    name = f"bmc_agent_error_cleanup_{probe}"
    source = "\n".join(
        [
            "/* Generated by kernel_driver_mode.py.",
            " * USB probe error-path harness: failed probe must not directly",
            " * free an object that was already published to userspace.",
            " */",
            f"int {name}(void)",
            "{",
            "  struct usb_interface __bmc_usbif = {0};",
            "  struct usb_device __bmc_usbdev = {0};",
            "  struct usb_device_id __bmc_id = {0};",
            "  __bmc_usbif.dev.parent = &__bmc_usbdev.dev;",
            "  bmc_agent_i2c_reset_registration_contract();",
            "  bmc_agent_clk_reset_contract();",
            "  bmc_agent_regulator_reset_contract();",
            "  bmc_agent_netdev_reset_contract();",
            "  bmc_agent_pm_runtime_reset_contract();",
            "  bmc_agent_usb_chrdev_reset_contract();",
            "  bmc_agent_usb_urb_reset_contract();",
            f"  int __bmc_ret = {probe}(&__bmc_usbif, &__bmc_id);",
            "  __CPROVER_assume(__bmc_ret != 0);",
            "  bmc_agent_devm_actions_teardown();",
            "  bmc_agent_usb_urb_lifecycle_balanced();",
            "  bmc_agent_regulator_lifecycle_balanced();",
            "  bmc_agent_pm_runtime_lifecycle_balanced();",
            "  return 0;",
            "}",
            "",
        ]
    )
    return GeneratedHarness(
        name=name,
        kind="usb_probe_error_cleanup",
        entrypoints=[probe],
        source=source,
        note="Generated from USB probe callback; checks failed-probe publish/free cleanup.",
    )


def _usb_netdev_disconnect_pattern(disconnect: str, parsed) -> dict[str, str] | None:
    """Infer a USB private object shape for netdev disconnect-order checks.

    This is intentionally source-pattern driven rather than case-specific.  It
    only fires for the common USB-netdev shape where disconnect retrieves a
    private object from ``usb_get_intfdata()``, derives ``priv->net``, and tears
    down NAPI/netdev state from that object.
    """
    body = (getattr(parsed, "function_bodies", {}) or {}).get(disconnect) or ""
    if not body:
        return None
    if "usb_get_intfdata" not in body or "unregister_netdev" not in body:
        return None
    if "netif_napi_del" not in body and "free_netdev" not in body:
        return None

    get_match = re.search(
        r"\b(?P<priv>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*usb_get_intfdata\s*\(",
        body,
    )
    if not get_match:
        return None
    priv_var = get_match.group("priv")

    decl_match = re.search(
        rf"\bstruct\s+(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\s*\*\s*{re.escape(priv_var)}\b",
        body,
    )
    if not decl_match:
        return None
    priv_type = f"struct {decl_match.group('tag')}"

    net_match = re.search(
        rf"\b(?P<net>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*{re.escape(priv_var)}\s*->\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*;",
        body,
    )
    if not net_match:
        return None
    net_var = net_match.group("net")
    net_field = net_match.group("field")
    if not re.search(rf"\bunregister_netdev\s*\(\s*{re.escape(net_var)}\s*\)", body):
        return None

    napi_match = re.search(
        rf"\bnetif_napi_del\s*\(\s*&\s*{re.escape(priv_var)}\s*->\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*\)",
        body,
    )
    napi_field = napi_match.group("field") if napi_match else ""

    return {
        "priv_var": priv_var,
        "priv_type": priv_type,
        "net_field": net_field,
        "napi_field": napi_field,
    }


def generated_usb_netdev_disconnect_harness(discovery: Discovery, parsed) -> GeneratedHarness | None:
    """Generate a lightweight USB disconnect harness for netdev order bugs.

    A full probe-success-then-disconnect path is the strongest model, but some
    real USB network probes are too large for a cheap candidate scan.  This
    harness models the framework state at the start of disconnect: the USB
    interface has driver data from a successful probe, the embedded netdev is
    registered, and NAPI has been added.  The source body still determines the
    actual teardown order.
    """
    disconnect = callback_name_for(discovery.entrypoints, "disconnect")
    if not disconnect:
        return None
    disconnect_sig = getattr(parsed, "functions", {}).get(disconnect)
    if not disconnect_sig or len(disconnect_sig.parameters) != 1:
        return None
    disconnect_if_base = pointer_base_type(disconnect_sig.parameters[0][0])
    if disconnect_if_base != "struct usb_interface":
        return None

    pattern = _usb_netdev_disconnect_pattern(disconnect, parsed)
    if not pattern:
        return None

    name = f"bmc_agent_disconnect_registered_netdev_{disconnect}"
    priv_type = pattern["priv_type"]
    net_field = pattern["net_field"]
    napi_field = pattern["napi_field"]
    napi_setup = (
        f"  bmc_agent_netif_napi_add(__bmc_net, &__bmc_priv->{napi_field}, 0);"
        if napi_field
        else "  /* No explicit netif_napi_del() source field in this disconnect path. */"
    )
    source = "\n".join(
        [
            "/* Generated by kernel_driver_mode.py.",
            " * USB netdev disconnect harness: model a successful probe state",
            " * and let the real disconnect callback perform teardown order.",
            " */",
            f"int {name}(void)",
            "{",
            "  struct usb_interface __bmc_usbif = {0};",
            "  struct usb_device __bmc_usbdev = {0};",
            f"  {priv_type} *__bmc_priv = ({priv_type} *)bmc_agent_valid_alloc(8192);",
            "  struct net_device *__bmc_net = (struct net_device *)bmc_agent_valid_alloc(8192);",
            "  __bmc_usbif.dev.parent = &__bmc_usbdev.dev;",
            f"  __bmc_priv->{net_field} = __bmc_net;",
            "  bmc_agent_usb_set_intfdata(&__bmc_usbif, __bmc_priv);",
            "  bmc_agent_netdev_reset_contract();",
            napi_setup,
            "  int __bmc_ret = bmc_agent_register_netdev(__bmc_net);",
            "  __CPROVER_assume(__bmc_ret == 0);",
            f"  (void){disconnect}(&__bmc_usbif);",
            "  bmc_agent_netdev_lifecycle_balanced();",
            "  return 0;",
            "}",
            "",
        ]
    )
    return GeneratedHarness(
        name=name,
        kind="usb_registered_netdev_disconnect",
        entrypoints=[disconnect],
        source=source,
        note="Generated from USB disconnect source pattern; models a prior successful netdev registration.",
    )


def generated_probe_suspend_harness(discovery: Discovery, parsed) -> GeneratedHarness | None:
    """Generate a probe-success-then-suspend harness for PM resource balance."""
    probe = callback_name_for(discovery.entrypoints, "probe")
    suspend = callback_name_for(discovery.entrypoints, "suspend")
    if not probe or not suspend:
        return None
    probe_sig = getattr(parsed, "functions", {}).get(probe)
    suspend_sig = getattr(parsed, "functions", {}).get(suspend)
    if not probe_sig or not suspend_sig:
        return None
    if len(probe_sig.parameters) != 1 or len(suspend_sig.parameters) != 1:
        return None
    if "int" not in re.sub(r"\s+", " ", probe_sig.return_type).split():
        return None
    probe_base = pointer_base_type(probe_sig.parameters[0][0])
    suspend_base = pointer_base_type(suspend_sig.parameters[0][0])
    if not probe_base or not suspend_base:
        return None
    if probe_base == suspend_base:
        suspend_arg = "&__bmc_dev"
    elif suspend_base == "struct device":
        suspend_arg = "&__bmc_dev.dev"
    else:
        return None

    name = f"bmc_agent_lifecycle_{probe}_then_{suspend}"
    source = "\n".join(
        [
            "/* Generated by kernel_driver_mode.py.",
            " * PM lifecycle harness: successful probe followed by suspend.",
            " */",
            f"int {name}(void)",
            "{",
            f"  {probe_base} __bmc_dev = {{0}};",
            *framework_object_setup_lines(probe_base, "__bmc_dev"),
            "  bmc_agent_i2c_reset_registration_contract();",
            "  bmc_agent_clk_reset_contract();",
            "  bmc_agent_regulator_reset_contract();",
            "  bmc_agent_netdev_reset_contract();",
            "  bmc_agent_pm_runtime_reset_contract();",
            f"  int __bmc_ret = {probe}(&__bmc_dev);",
            "  __CPROVER_assume(__bmc_ret == 0);",
            "  bmc_agent_i2c_success_requires_registration();",
            f"  (void){suspend}({suspend_arg});",
            "  return 0;",
            "}",
            "",
        ]
    )
    return GeneratedHarness(
        name=name,
        kind="probe_success_then_suspend",
        entrypoints=[probe, suspend],
        source=source,
        note="Generated from callback table; checks PM suspend after successful probe.",
    )


def _pm_runtime_autosuspend_remove_pattern(probe: str, remove: str, parsed) -> dict[str, str] | None:
    """Infer the common autosuspend teardown-underflow pattern.

    Some platform drivers call ``pm_runtime_use_autosuspend()`` during probe
    and later remove the device after autosuspend may already have dropped the
    usage count to zero.  A remove-side ``pm_runtime_put_sync()`` is then a
    potential underflow, while ``pm_runtime_dont_use_autosuspend()`` is the
    matching teardown operation.  This detector is source-pattern based and
    deliberately avoids commit/case names.
    """
    bodies = getattr(parsed, "function_bodies", {}) or {}
    probe_body = bodies.get(probe) or ""
    remove_body = bodies.get(remove) or ""
    if not probe_body or not remove_body:
        return None
    if "pm_runtime_use_autosuspend" not in probe_body:
        return None
    if "pm_runtime_put_sync" not in remove_body and "pm_runtime_dont_use_autosuspend" not in remove_body:
        return None

    private_type = ""
    private_var = ""
    skip_negative_field = ""
    drvdata_match = re.search(
        r"\bstruct\s+(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\s*\*\s*"
        r"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
        r"(?:dev_get_drvdata|platform_get_drvdata)\s*\(",
        remove_body,
    )
    if drvdata_match:
        private_type = f"struct {drvdata_match.group('tag')}"
        private_var = drvdata_match.group("var")
        branch_match = re.search(
            rf"\b{re.escape(private_var)}\s*->\s*"
            r"(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*<\s*0",
            remove_body,
        )
        if branch_match:
            skip_negative_field = branch_match.group("field")

    return {
        "private_type": private_type,
        "private_var": private_var,
        "skip_negative_field": skip_negative_field,
    }


def generated_pm_runtime_autosuspend_remove_harness(discovery: Discovery, parsed) -> GeneratedHarness | None:
    """Generate a remove-side runtime-PM autosuspend underflow harness.

    The harness models the framework state after a successful probe configured
    autosuspend. It does not execute the full probe because media drivers often
    have large firmware/V4L2 registration paths; the real remove callback still
    determines whether the teardown operation is valid.
    """
    probe = callback_name_for(discovery.entrypoints, "probe")
    remove = callback_name_for(discovery.entrypoints, "remove")
    if not probe or not remove:
        return None
    probe_sig = getattr(parsed, "functions", {}).get(probe)
    remove_sig = getattr(parsed, "functions", {}).get(remove)
    if not probe_sig or not remove_sig:
        return None
    if len(probe_sig.parameters) != 1 or len(remove_sig.parameters) != 1:
        return None
    probe_base = pointer_base_type(probe_sig.parameters[0][0])
    remove_base = pointer_base_type(remove_sig.parameters[0][0])
    if not probe_base or probe_base != remove_base:
        return None

    pattern = _pm_runtime_autosuspend_remove_pattern(probe, remove, parsed)
    if not pattern:
        return None

    name = f"bmc_agent_autosuspend_remove_{remove}"
    setup_lines = [
        f"  {probe_base} __bmc_dev = {{0}};",
        *framework_object_setup_lines(probe_base, "__bmc_dev"),
        "  bmc_agent_pm_runtime_reset_contract();",
        "  bmc_agent_pm_runtime_use_autosuspend(&__bmc_dev.dev);",
        "  bmc_agent_pm_runtime_enable(&__bmc_dev.dev);",
        "  bmc_agent_pm_runtime_model_autosuspended(&__bmc_dev.dev);",
    ]
    private_type = pattern.get("private_type") or ""
    if private_type:
        setup_lines.extend(
            [
                f"  {private_type} *__bmc_priv = ({private_type} *)bmc_agent_valid_alloc(8192);",
                "  __CPROVER_assume(__bmc_priv != (void *)0);",
                "  bmc_agent_dev_set_drvdata(&__bmc_dev.dev, __bmc_priv);",
            ]
        )
        field = pattern.get("skip_negative_field") or ""
        if field:
            setup_lines.append(f"  __bmc_priv->{field} = 0;")

    source = "\n".join(
        [
            "/* Generated by kernel_driver_mode.py.",
            " * Runtime-PM autosuspend remove harness: model a successful",
            " * probe state where autosuspend may already have dropped the",
            " * usage count before the real remove callback runs.",
            " */",
            f"int {name}(void)",
            "{",
            *setup_lines,
            f"  (void){remove}(&__bmc_dev);",
            "  bmc_agent_pm_runtime_lifecycle_balanced();",
            "  return 0;",
            "}",
            "",
        ]
    )
    return GeneratedHarness(
        name=name,
        kind="pm_runtime_autosuspend_remove",
        entrypoints=[probe, remove],
        source=source,
        note="Generated from probe/remove source pattern; models autosuspended runtime-PM remove.",
    )


def generated_lifecycle_harnesses(discovery: Discovery, parsed) -> list[GeneratedHarness]:
    """Generate generic driver lifecycle harnesses from callback discovery.

    These harnesses are deliberately framework-shaped, not CVE-shaped.  They
    model how a bus invokes driver callbacks (for example probe success followed
    by remove), so cross-callback resource-lifetime bugs have a state path.
    """
    harnesses: list[GeneratedHarness] = []
    usb = generated_usb_lifecycle_harness(discovery, parsed)
    if usb is not None:
        usb_netdev_disconnect = generated_usb_netdev_disconnect_harness(discovery, parsed)
        if usb_netdev_disconnect is not None:
            harnesses.append(usb_netdev_disconnect)
        harnesses.append(usb)
        pm_suspend = generated_probe_suspend_harness(discovery, parsed)
        if pm_suspend is not None:
            harnesses.append(pm_suspend)
        usb_probe_error = generated_usb_probe_error_cleanup_harness(discovery, parsed)
        if usb_probe_error is not None:
            harnesses.append(usb_probe_error)
        harnesses.extend(generated_error_cleanup_harnesses(discovery, parsed))
        return harnesses

    probe = callback_name_for(discovery.entrypoints, "probe")
    remove = (
        callback_name_for(discovery.entrypoints, "remove")
        or callback_name_for(discovery.entrypoints, "disconnect")
        or callback_name_for(discovery.entrypoints, "shutdown")
    )
    if not probe or not remove:
        autosuspend_remove = generated_pm_runtime_autosuspend_remove_harness(discovery, parsed)
        if autosuspend_remove is not None:
            harnesses.append(autosuspend_remove)
        pm_suspend = generated_probe_suspend_harness(discovery, parsed)
        if pm_suspend is not None:
            harnesses.append(pm_suspend)
        harnesses.extend(generated_error_cleanup_harnesses(discovery, parsed))
        probe_error = generated_probe_error_cleanup_harness(discovery, parsed)
        if probe_error is not None:
            harnesses.append(probe_error)
        return harnesses
    probe_sig = getattr(parsed, "functions", {}).get(probe)
    remove_sig = getattr(parsed, "functions", {}).get(remove)
    if not probe_sig or not remove_sig:
        autosuspend_remove = generated_pm_runtime_autosuspend_remove_harness(discovery, parsed)
        if autosuspend_remove is not None:
            harnesses.append(autosuspend_remove)
        pm_suspend = generated_probe_suspend_harness(discovery, parsed)
        if pm_suspend is not None:
            harnesses.append(pm_suspend)
        harnesses.extend(generated_error_cleanup_harnesses(discovery, parsed))
        probe_error = generated_probe_error_cleanup_harness(discovery, parsed)
        if probe_error is not None:
            harnesses.append(probe_error)
        return harnesses
    if len(probe_sig.parameters) != 1 or len(remove_sig.parameters) != 1:
        autosuspend_remove = generated_pm_runtime_autosuspend_remove_harness(discovery, parsed)
        if autosuspend_remove is not None:
            harnesses.append(autosuspend_remove)
        pm_suspend = generated_probe_suspend_harness(discovery, parsed)
        if pm_suspend is not None:
            harnesses.append(pm_suspend)
        harnesses.extend(generated_error_cleanup_harnesses(discovery, parsed))
        probe_error = generated_probe_error_cleanup_harness(discovery, parsed)
        if probe_error is not None:
            harnesses.append(probe_error)
        return harnesses
    probe_base = pointer_base_type(probe_sig.parameters[0][0])
    remove_base = pointer_base_type(remove_sig.parameters[0][0])
    if not probe_base or probe_base != remove_base:
        autosuspend_remove = generated_pm_runtime_autosuspend_remove_harness(discovery, parsed)
        if autosuspend_remove is not None:
            harnesses.append(autosuspend_remove)
        pm_suspend = generated_probe_suspend_harness(discovery, parsed)
        if pm_suspend is not None:
            harnesses.append(pm_suspend)
        harnesses.extend(generated_error_cleanup_harnesses(discovery, parsed))
        probe_error = generated_probe_error_cleanup_harness(discovery, parsed)
        if probe_error is not None:
            harnesses.append(probe_error)
        return harnesses

    name = f"bmc_agent_lifecycle_{probe}_then_{remove}"
    probe_returns_int = "int" in re.sub(r"\s+", " ", probe_sig.return_type).split()
    if probe_returns_int:
        probe_call = (
            "  bmc_agent_i2c_reset_registration_contract();\n"
            "  bmc_agent_clk_reset_contract();\n"
            "  bmc_agent_regulator_reset_contract();\n"
            "  bmc_agent_netdev_reset_contract();\n"
            "  bmc_agent_pm_runtime_reset_contract();\n"
            f"  int __bmc_ret = {probe}(&__bmc_dev);\n"
            "  __CPROVER_assume(__bmc_ret == 0);\n"
            "  bmc_agent_i2c_success_requires_registration();"
        )
    else:
        probe_call = (
            "  bmc_agent_i2c_reset_registration_contract();\n"
            "  bmc_agent_clk_reset_contract();\n"
            "  bmc_agent_regulator_reset_contract();\n"
            "  bmc_agent_netdev_reset_contract();\n"
            "  bmc_agent_pm_runtime_reset_contract();\n"
            f"  {probe}(&__bmc_dev);\n"
            "  bmc_agent_i2c_success_requires_registration();"
        )
    source = "\n".join(
        [
            "/* Generated by kernel_driver_mode.py.",
            " * Generic bus lifecycle harness: successful probe followed by teardown.",
            " */",
            f"int {name}(void)",
            "{",
            f"  {probe_base} __bmc_dev = {{0}};",
            *framework_object_setup_lines(probe_base, "__bmc_dev"),
            probe_call,
            f"  (void){remove}(&__bmc_dev);",
            "  bmc_agent_devm_actions_teardown();",
            "  bmc_agent_clk_lifecycle_balanced();",
            "  bmc_agent_regulator_lifecycle_balanced();",
            "  bmc_agent_netdev_lifecycle_balanced();",
            "  bmc_agent_pm_runtime_lifecycle_balanced();",
            "  return 0;",
            "}",
            "",
        ]
    )
    autosuspend_remove = generated_pm_runtime_autosuspend_remove_harness(discovery, parsed)
    if autosuspend_remove is not None:
        harnesses.append(autosuspend_remove)
    harnesses.append(
        GeneratedHarness(
            name=name,
            kind="probe_success_then_teardown",
            entrypoints=[probe, remove],
            source=source,
            note="Generated from callback table; does not use case target_function metadata.",
        )
    )
    pm_suspend = generated_probe_suspend_harness(discovery, parsed)
    if pm_suspend is not None:
        harnesses.append(pm_suspend)
    harnesses.extend(generated_error_cleanup_harnesses(discovery, parsed))
    probe_error = generated_probe_error_cleanup_harness(discovery, parsed)
    if probe_error is not None:
        harnesses.append(probe_error)
    return harnesses


def _is_usb_device_id_pointer(param_type: str) -> bool:
    return bool(
        re.search(r"\bstruct\s+usb_device_id\b", param_type)
        and "*" in param_type
    )


def generated_usb_lifecycle_harness(discovery: Discovery, parsed) -> GeneratedHarness | None:
    """Generate a generic USB probe-success-then-disconnect harness.

    USB probe callbacks receive ``(struct usb_interface *,
    const struct usb_device_id *)`` while disconnect callbacks receive only the
    interface.  The generic same-type lifecycle harness cannot model that
    boundary.  This harness still avoids case-specific replay: it builds the
    ordinary USB callback objects and leaves descriptor fields such as
    ``usbdev.serial`` nondeterministically present or absent.
    """
    probe = callback_name_for(discovery.entrypoints, "probe")
    disconnect = callback_name_for(discovery.entrypoints, "disconnect")
    if not probe or not disconnect:
        return None
    probe_sig = getattr(parsed, "functions", {}).get(probe)
    disconnect_sig = getattr(parsed, "functions", {}).get(disconnect)
    if not probe_sig or not disconnect_sig:
        return None
    if len(probe_sig.parameters) != 2 or len(disconnect_sig.parameters) != 1:
        return None
    probe_if_base = pointer_base_type(probe_sig.parameters[0][0])
    disconnect_if_base = pointer_base_type(disconnect_sig.parameters[0][0])
    if probe_if_base != "struct usb_interface" or disconnect_if_base != "struct usb_interface":
        return None
    if not _is_usb_device_id_pointer(probe_sig.parameters[1][0]):
        return None

    name = f"bmc_agent_lifecycle_{probe}_then_{disconnect}"
    probe_returns_int = "int" in re.sub(r"\s+", " ", probe_sig.return_type).split()
    if probe_returns_int:
        probe_call = (
            "  bmc_agent_i2c_reset_registration_contract();\n"
            "  bmc_agent_clk_reset_contract();\n"
            "  bmc_agent_regulator_reset_contract();\n"
            "  bmc_agent_netdev_reset_contract();\n"
            "  bmc_agent_pm_runtime_reset_contract();\n"
            f"  int __bmc_ret = {probe}(&__bmc_usbif, &__bmc_id);\n"
            "  __CPROVER_assume(__bmc_ret == 0);\n"
            "  bmc_agent_i2c_success_requires_registration();"
        )
    else:
        probe_call = (
            "  bmc_agent_i2c_reset_registration_contract();\n"
            "  bmc_agent_clk_reset_contract();\n"
            "  bmc_agent_regulator_reset_contract();\n"
            "  bmc_agent_netdev_reset_contract();\n"
            "  bmc_agent_pm_runtime_reset_contract();\n"
            f"  {probe}(&__bmc_usbif, &__bmc_id);\n"
            "  bmc_agent_i2c_success_requires_registration();"
        )
    source = "\n".join(
        [
            "/* Generated by kernel_driver_mode.py.",
            " * Generic USB lifecycle harness: probe callback with framework",
            " * objects followed by disconnect. Device descriptor fields are",
            " * intentionally nondeterministic, not case-specific.",
            " */",
            f"int {name}(void)",
            "{",
            "  struct usb_interface __bmc_usbif = {0};",
            "  struct usb_device __bmc_usbdev = {0};",
            "  struct usb_device_id __bmc_id = {0};",
            "  __bmc_usbif.dev.parent = &__bmc_usbdev.dev;",
            "  if (__CPROVER_nondet_bool())",
            "    __bmc_usbdev.serial = (char *)bmc_agent_valid_alloc(16);",
            "  else",
            "    __bmc_usbdev.serial = (char *)0;",
            "  bmc_agent_usb_chrdev_reset_contract();",
            "  bmc_agent_usb_urb_reset_contract();",
            probe_call,
            f"  (void){disconnect}(&__bmc_usbif);",
            "  bmc_agent_devm_actions_teardown();",
            "  bmc_agent_usb_urb_lifecycle_balanced();",
            "  bmc_agent_clk_lifecycle_balanced();",
            "  bmc_agent_regulator_lifecycle_balanced();",
            "  bmc_agent_netdev_lifecycle_balanced();",
            "  bmc_agent_pm_runtime_lifecycle_balanced();",
            "  return 0;",
            "}",
            "",
        ]
    )
    return GeneratedHarness(
        name=name,
        kind="usb_probe_success_then_disconnect",
        entrypoints=[probe, disconnect],
        source=source,
        note="Generated from USB callback table; does not use case entry_wrapper metadata.",
    )


def summarize_context_packet(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    if not packet:
        return None
    contracts = packet.get("kernel_api_contracts") or []
    candidate_properties = packet.get("candidate_properties") or []
    return {
        "case_id": packet.get("case_id"),
        "variant": packet.get("variant"),
        "entrypoint_count": len(packet.get("entrypoints") or []),
        "function_count": len(packet.get("functions") or []),
        "kernel_api_count": len(packet.get("kernel_api_calls") or {}),
        "contract_count": len(contracts),
        "candidate_property_count": len(candidate_properties),
        "candidate_properties": [
            {
                "id": prop.get("id"),
                "property": prop.get("property"),
                "confidence": prop.get("confidence"),
            }
            for prop in candidate_properties[:10]
        ],
        "candidate_triggered_contracts": [
            contract.get("id")
            for contract in contracts
            if contract.get("status") == "candidate_triggered"
        ],
    }


def discover(package: Path, *, include_suffix_fallback: bool = True, max_public: int = 12) -> Discovery:
    pkg = load_package(package)
    parsed, text = parse_driver_context(pkg.analysis_source)
    all_names = set(getattr(parsed, "functions", {}).keys())
    wrappers = sorted(name for name in all_names if name.startswith(WRAPPER_PREFIX))
    driver_functions = {
        name for name in all_names
        if is_driver_function(name) and name not in GENERATED_SHIM_FUNCTIONS
    }

    callback = callback_entries(text, driver_functions)
    module = module_entries(text, driver_functions)
    exported = exported_entries(text, parsed, driver_functions)
    packet = context_packet_entries(pkg.context_packet, driver_functions)
    suffix = suffix_entries(driver_functions) if include_suffix_fallback else []

    # Strong framework callbacks are the normal whole-driver roots. Public
    # functions are fallback roots for driver-library files such as adis.c,
    # where the real callers live in other modules rather than a local
    # platform_driver table.
    primary = dedupe_entries([*callback, *module])
    warnings: list[str] = []
    public_entries = [
        e for e in dedupe_entries(exported)
        if e.kind in {"exported_symbol", "public_function"}
    ]
    has_strong = any(entry.score >= 90 for entry in primary)
    if len(public_entries) > max_public and not has_strong:
        warnings.append(
            f"public-function discovery found {len(public_entries)} entrypoints; capped to {max_public}"
        )
        public_entries = public_entries[:max_public]
    if has_strong:
        entries = dedupe_entries([*primary, *packet])
    else:
        entries = dedupe_entries([*primary, *packet, *public_entries])
    if not entries:
        entries = dedupe_entries(suffix)
    if not entries:
        warnings.append("no callback/module/public/suffix entrypoints discovered")

    closure = driver_closure(
        (entry.name for entry in entries),
        getattr(parsed, "call_graph", {}),
        driver_functions,
    )
    external = external_symbols_for(closure, getattr(parsed, "call_graph", {}), driver_functions)

    return Discovery(
        package=str(pkg.package),
        source_i=str(pkg.source_i),
        analysis_source=str(pkg.analysis_source),
        driver_functions=sorted(driver_functions),
        entrypoints=entries,
        driver_closure=sorted(closure),
        external_symbols=sorted(external),
        context_packet_summary=summarize_context_packet(pkg.context_packet),
        ignored_wrappers=wrappers,
        warnings=warnings,
    )


def prune_parsed_to_driver_scope(parsed, driver_scope: set[str]):
    """Remove non-driver function definitions while preserving type context."""
    for attr in ("functions", "function_bodies", "function_definitions", "function_source_files"):
        mapping = getattr(parsed, attr, None)
        if isinstance(mapping, dict):
            for name in list(mapping.keys()):
                if name not in driver_scope:
                    mapping.pop(name, None)
    call_graph = getattr(parsed, "call_graph", None)
    if isinstance(call_graph, dict):
        for name in list(call_graph.keys()):
            if name not in driver_scope:
                call_graph.pop(name, None)
    address_taken = getattr(parsed, "address_taken_in", None)
    if isinstance(address_taken, dict):
        for name in list(address_taken.keys()):
            if name not in driver_scope:
                address_taken.pop(name, None)
            else:
                address_taken[name] = {caller for caller in address_taken[name] if caller in driver_scope}
    return parsed


@contextlib.contextmanager
def kernel_scope_patch(
    source_i: Path,
    driver_scope: set[str],
    *,
    driver_entrypoints: list[EntryPoint] | None = None,
):
    """Patch parser entrypoints used by AMCPipeline for one run."""
    import bmc_agent.pipeline as pipeline_mod
    import bmc_agent.source_parser as source_parser_mod

    orig_pipeline_parse = pipeline_mod.parse_source_file
    orig_source_parse = source_parser_mod.parse_source_file
    source_i_resolved = source_i.resolve()

    def scoped_parse(path, source_text=None):
        parsed = orig_source_parse(path, source_text=source_text)
        try:
            same_source = Path(path).resolve() == source_i_resolved
        except Exception:
            same_source = False
        if same_source:
            parsed = prune_parsed_to_driver_scope(parsed, driver_scope)
            parsed.bmc_agent_driver_entrypoints = [
                entry.__dict__ for entry in (driver_entrypoints or [])
            ]
        return parsed

    pipeline_mod.parse_source_file = scoped_parse
    source_parser_mod.parse_source_file = scoped_parse
    try:
        yield
    finally:
        pipeline_mod.parse_source_file = orig_pipeline_parse
        source_parser_mod.parse_source_file = orig_source_parse


@contextlib.contextmanager
def lifecycle_inline_patch(force_inline: set[str]):
    """Force lifecycle callback bodies to stay real inside generated harnesses."""
    if not force_inline:
        yield
        return
    import bmc_agent.harness_generator as harness_mod

    orig_should_inline = harness_mod._should_inline_callee

    def patched_should_inline(callee_name, parsed_file, max_loc=30, size_helper_max_loc=200):
        if callee_name in force_inline:
            return True, "forced inline for generated kernel-driver lifecycle harness"
        return orig_should_inline(
            callee_name,
            parsed_file,
            max_loc=max_loc,
            size_helper_max_loc=size_helper_max_loc,
        )

    harness_mod._should_inline_callee = patched_should_inline
    try:
        yield
    finally:
        harness_mod._should_inline_callee = orig_should_inline


@contextlib.contextmanager
def no_external_llm_patch(enabled: bool):
    """Disable all external LLM calls for one kernel-driver run.

    ``--minimal`` is meant to be a low-cost CBMC/environment-model pass. Some
    BMC-Agent stages can still fall back to LLM-generated reproducers even when
    the main realism/dynamic-validation knobs are disabled. Keep this guard
    local to the kernel-driver experiment wrapper so the main pipeline behavior
    is unchanged.
    """
    if not enabled:
        yield
        return

    import bmc_agent.cex_validator as cex_validator_mod
    import bmc_agent.llm as llm_mod

    orig_generate_reproducer = cex_validator_mod.CExValidator._generate_system_entry_reproducer
    orig_complete = llm_mod.LLMClient.complete
    orig_complete_with_tools = getattr(llm_mod.LLMClient, "complete_with_tools", None)

    def no_llm_reproducer(self, call_chain, counterexample, all_funcs, parsed_file):
        chain_str = " -> ".join(call_chain or [])
        return (
            "// UNREPRODUCIBLE: external LLM reproducer generation disabled "
            f"by linux_driver_kernel_mode --minimal; call chain was {chain_str}"
        )

    def blocked_complete(self, *args, **kwargs):
        raise llm_mod.LLMError(
            "external LLM calls disabled by linux_driver_kernel_mode --minimal"
        )

    cex_validator_mod.CExValidator._generate_system_entry_reproducer = no_llm_reproducer
    llm_mod.LLMClient.complete = blocked_complete
    if orig_complete_with_tools is not None:
        llm_mod.LLMClient.complete_with_tools = blocked_complete
    try:
        yield
    finally:
        cex_validator_mod.CExValidator._generate_system_entry_reproducer = orig_generate_reproducer
        llm_mod.LLMClient.complete = orig_complete
        if orig_complete_with_tools is not None:
            llm_mod.LLMClient.complete_with_tools = orig_complete_with_tools


def build_manifest(discovery: Discovery, *, command: list[str] | None = None) -> dict[str, Any]:
    data = discovery.to_dict()
    data["mode"] = {
        "name": "linux_driver_kernel_aware",
        "kernel_side_policy": (
            "Only functions parsed from analysis_context.c are treated as "
            "driver-local. Kernel functions remain external environment symbols "
            "and are not spec-generated or checked as Phase-2 roots."
        ),
        "target_function_policy": (
            "Known CVE target_function metadata and bmc_agent_entry_* wrappers "
            "are ignored for entrypoint discovery."
        ),
    }
    if command:
        data["command"] = command
    return data


_KERNEL_HELPER_NAMES = {
    "ERR_PTR",
    "PTR_ERR",
    "IS_ERR",
    "IS_ERR_OR_NULL",
    "PTR_ERR_OR_ZERO",
}


def _source_defines_function(source_text: str, name: str) -> bool:
    """Return whether a preprocessed source already defines ``name``.

    Linux .i files often carry static-inline helpers such as IS_ERR. The
    driver-mode prelude should not redeclare those helpers with a simplified
    type, because Phase-3 feasibility harnesses reassemble type declarations
    and CBMC rejects duplicate helper definitions.
    """
    pattern = re.compile(
        rf"^\s*(?!typedef\b)(?!extern\b)(?!.*;\s*$)[^\n{{}};]*\b{re.escape(name)}\s*"
        rf"\([^;{{}}]*\)\s*(?:\n\s*)?\{{",
        re.MULTILINE,
    )
    return bool(pattern.search(source_text))


def kernel_env_prelude(
    discovery: Discovery,
    *,
    source_text: str | None = None,
    devm_regulator_callbacks: set[str] | None = None,
) -> str:
    """Return a small declaration-only kernel environment prelude.

    These are not target functions. They keep CBMC from failing conversion on
    kernel API symbols that are used by the driver but intentionally excluded
    from driver-local analysis.
    """
    external = set(discovery.external_symbols)
    devm_regulator_callbacks = set(devm_regulator_callbacks or set())
    devm_regulator_callback_decls = [
        f"static void {name}(void *);"
        for name in sorted(devm_regulator_callbacks)
    ]
    devm_regulator_action_condition = " || ".join(
        f"action == {name}" for name in sorted(devm_regulator_callbacks)
    ) or "0"
    lines = [
        "/* BMC-Agent kernel-aware driver-mode environment prelude.",
        " * Generated by experiments/linux_driver_kernel_mode/kernel_driver_mode.py.",
        " * Kernel API symbols are declarations/models only; they are not",
        " * BMC-Agent spec-generation or Phase-2 verification roots.",
        " */",
        "#ifndef BMC_AGENT_KERNEL_DRIVER_MODE_ENV",
        "#define BMC_AGENT_KERNEL_DRIVER_MODE_ENV",
        "typedef unsigned long bmc_agent_kernel_size_t;",
        "typedef unsigned int bmc_agent_kernel_uint_t;",
        "typedef unsigned char bmc_agent_kernel_u8_t;",
        "struct device;",
        "struct platform_device;",
        "struct usb_interface;",
        "struct usb_device;",
        "struct i2c_client;",
        "struct i2c_adapter;",
        "struct clk;",
        "struct clk_hw;",
        "struct regulator;",
        *devm_regulator_callback_decls,
        "struct gpio_desc;",
        "struct iio_dev;",
        "struct device_node;",
        "struct of_phandle_args;",
        "struct spi_controller;",
        "struct spi_message;",
        "struct spi_transfer;",
        "struct pci_dev;",
        "struct net_device;",
        "struct napi_struct;",
        "struct urb;",
        "struct usb_anchor;",
        "struct timer_list;",
        "struct delayed_work;",
        "struct phylink;",
        "void *__CPROVER_allocate(bmc_agent_kernel_size_t size, int zero);",
        "_Bool __CPROVER_nondet_bool(void) { _Bool r; return r; }",
        "int __CPROVER_nondet_int(void) { int r; return r; }",
        "unsigned int __CPROVER_nondet_uint(void) { unsigned int r; return r; }",
        "long __CPROVER_nondet_long(void) { long r; return r; }",
        "unsigned long __CPROVER_nondet_ulong(void) { unsigned long r; return r; }",
        "void *bmc_agent_valid_alloc(bmc_agent_kernel_size_t size) {",
        "  if (!size) size = 1;",
        "  return __CPROVER_allocate(size, 1);",
        "}",
        "unsigned long size_mul(unsigned long a, unsigned long b) { return a * b; }",
        "int mem_alloc_profiling_enabled(void) { return 0; }",
        "void *kzalloc_noprof(unsigned long size, unsigned int flags) { (void)flags; return bmc_agent_valid_alloc(size); }",
        "void *kmalloc_noprof(unsigned long size, unsigned int flags) { (void)flags; return bmc_agent_valid_alloc(size); }",
        "void *bmc_agent_err_ptr(long error) { return (void *)error; }",
        "long bmc_agent_ptr_err(const void *ptr) { return (long)ptr; }",
        "int bmc_agent_is_err(const void *ptr) {",
        "  unsigned long x = (unsigned long)ptr;",
        "  return x >= (unsigned long)-4095;",
        "}",
        "int bmc_agent_is_err_or_null(const void *ptr) { return !ptr || bmc_agent_is_err(ptr); }",
        "int bmc_agent_ptr_err_or_zero(const void *ptr) { return bmc_agent_is_err(ptr) ? (int)bmc_agent_ptr_err(ptr) : 0; }",
        "struct usb_device *__intf_to_usbdev(struct usb_interface *intf);",
        "void *bmc_agent_ptr_or_err(void) {",
        "  if (__CPROVER_nondet_bool()) return bmc_agent_err_ptr(-12);",
        "  return bmc_agent_valid_alloc(4096);",
        "}",
        "int bmc_agent_kernel_status(void) {",
        "  int r = __CPROVER_nondet_int();",
        "  __CPROVER_assume(r == 0 || (r < 0 && r >= -4095));",
        "  return r;",
        "}",
        "static void *bmc_agent_platform_drvdata_slot;",
        "static int bmc_agent_platform_drvdata_set;",
        "static void *bmc_agent_device_drvdata_slot;",
        "static int bmc_agent_device_drvdata_set;",
        "static void *bmc_agent_usb_intfdata_slot;",
        "static int bmc_agent_usb_intfdata_set;",
        "static void *bmc_agent_usb_chrdev_payload;",
        "static int bmc_agent_usb_chrdev_open_ref;",
        "static void *bmc_agent_usb_urb_slot;",
        "static int bmc_agent_usb_urb_owned;",
        "static void *bmc_agent_netdev_slot;",
        "static int bmc_agent_netdev_registered;",
        "static int bmc_agent_netdev_napi_added;",
        "static void *bmc_agent_devm_spi_controller_slot;",
        "static void *bmc_agent_manual_spi_controller_slot;",
        "static int bmc_agent_spi_suspend_failed;",
        "static void *bmc_agent_i2c_adapter_slot;",
        "static int bmc_agent_i2c_registration_failed;",
        "static void *bmc_agent_clk_slot;",
        "static int bmc_agent_clk_prepare_count;",
        "static int bmc_agent_clk_enable_count;",
        "static void (*bmc_agent_devm_action_slot)(void *);",
        "static void *bmc_agent_devm_action_data_slot;",
        "static int bmc_agent_devm_action_registered;",
        "static void *bmc_agent_regulator_known_slot;",
        "static void *bmc_agent_regulator_slot;",
        "static int bmc_agent_regulator_enable_count;",
        "static void *bmc_agent_pm_runtime_dev_slot;",
        "static int bmc_agent_pm_runtime_enabled;",
        "static int bmc_agent_pm_runtime_autosuspend_enabled;",
        "static int bmc_agent_pm_runtime_usage_count;",
        "void bmc_agent_platform_set_drvdata(struct platform_device *pdev, void *data) { (void)pdev; bmc_agent_platform_drvdata_slot = data; bmc_agent_platform_drvdata_set = 1; }",
        "void *bmc_agent_platform_get_drvdata(const struct platform_device *pdev) { (void)pdev; return bmc_agent_platform_drvdata_set ? bmc_agent_platform_drvdata_slot : bmc_agent_valid_alloc(64); }",
        "void bmc_agent_dev_set_drvdata(struct device *dev, void *data) { (void)dev; bmc_agent_device_drvdata_slot = data; bmc_agent_device_drvdata_set = 1; }",
        "void *bmc_agent_dev_get_drvdata(const struct device *dev) { (void)dev; return bmc_agent_device_drvdata_set ? bmc_agent_device_drvdata_slot : bmc_agent_valid_alloc(64); }",
        "void bmc_agent_i2c_set_clientdata(void *client, void *data) { (void)client; bmc_agent_device_drvdata_slot = data; bmc_agent_device_drvdata_set = 1; }",
        "void bmc_agent_usb_set_intfdata(struct usb_interface *intf, void *data) { (void)intf; bmc_agent_usb_intfdata_slot = data; bmc_agent_usb_intfdata_set = 1; }",
        "void *bmc_agent_usb_get_intfdata(struct usb_interface *intf) { (void)intf; return bmc_agent_usb_intfdata_set ? bmc_agent_usb_intfdata_slot : bmc_agent_valid_alloc(64); }",
        "void bmc_agent_usb_chrdev_reset_contract(void) { bmc_agent_usb_chrdev_payload = (void *)0; bmc_agent_usb_chrdev_open_ref = 0; }",
        "int bmc_agent_usb_register_dev(struct usb_interface *intf, const struct usb_class_driver *class_driver) { (void)intf; (void)class_driver; int r = bmc_agent_kernel_status(); if (r == 0) { bmc_agent_usb_chrdev_payload = bmc_agent_usb_intfdata_slot; bmc_agent_usb_chrdev_open_ref = __CPROVER_nondet_bool(); } return r; }",
        "void bmc_agent_usb_deregister_dev(struct usb_interface *intf, const struct usb_class_driver *class_driver) { (void)intf; (void)class_driver; }",
        "void bmc_agent_kfree(const void *ptr) { __CPROVER_assert(!(ptr != (void *)0 && ptr == bmc_agent_usb_chrdev_payload && bmc_agent_usb_chrdev_open_ref), \"freeing usb-registered object before final release\"); }",
        "void bmc_agent_usb_urb_reset_contract(void) { bmc_agent_usb_urb_slot = (void *)0; bmc_agent_usb_urb_owned = 0; }",
        "struct urb *bmc_agent_usb_alloc_urb(int iso_packets, unsigned int mem_flags) { (void)iso_packets; (void)mem_flags; struct urb *urb = (struct urb *)bmc_agent_valid_alloc(1024); bmc_agent_usb_urb_slot = urb; bmc_agent_usb_urb_owned = 1; return urb; }",
        "int bmc_agent_usb_submit_urb(struct urb *urb, unsigned int mem_flags) { (void)urb; (void)mem_flags; return bmc_agent_kernel_status(); }",
        "void bmc_agent_usb_free_urb(struct urb *urb) { if (urb == bmc_agent_usb_urb_slot) { __CPROVER_assert(bmc_agent_usb_urb_owned, \"usb_free_urb without owned URB\"); bmc_agent_usb_urb_owned = 0; bmc_agent_usb_urb_slot = (void *)0; } }",
        "void bmc_agent_usb_kill_urb(struct urb *urb) { (void)urb; }",
        "void bmc_agent_usb_urb_lifecycle_balanced(void) { __CPROVER_assert(!bmc_agent_usb_urb_owned, \"probe error path leaks allocated URB\"); }",
        "void bmc_agent_netdev_reset_contract(void) { bmc_agent_netdev_slot = (void *)0; bmc_agent_netdev_registered = 0; bmc_agent_netdev_napi_added = 0; }",
        "void bmc_agent_netif_napi_add(struct net_device *dev, struct napi_struct *napi, int (*poll)(struct napi_struct *, int)) { (void)napi; (void)poll; bmc_agent_netdev_slot = dev; bmc_agent_netdev_napi_added = 1; }",
        "void bmc_agent_netif_napi_add_weight(struct net_device *dev, struct napi_struct *napi, int (*poll)(struct napi_struct *, int), int weight) { (void)weight; bmc_agent_netif_napi_add(dev, napi, poll); }",
        "int bmc_agent_register_netdev(struct net_device *dev) { int r = bmc_agent_kernel_status(); if (r == 0) { bmc_agent_netdev_slot = dev; bmc_agent_netdev_registered = 1; } return r; }",
        "void bmc_agent_netif_napi_del(struct napi_struct *napi) { (void)napi; __CPROVER_assert(!bmc_agent_netdev_registered, \"manual netif_napi_del before unregister_netdev\"); bmc_agent_netdev_napi_added = 0; }",
        "void bmc_agent_unregister_netdev(struct net_device *dev) { if (dev == bmc_agent_netdev_slot) { bmc_agent_netdev_registered = 0; bmc_agent_netdev_napi_added = 0; } }",
        "void bmc_agent_free_netdev(struct net_device *dev) { __CPROVER_assert(!(dev == bmc_agent_netdev_slot && bmc_agent_netdev_registered), \"free_netdev while netdev registered\"); if (dev == bmc_agent_netdev_slot) bmc_agent_netdev_slot = (void *)0; }",
        "void bmc_agent_netdev_lifecycle_balanced(void) { __CPROVER_assert(!bmc_agent_netdev_registered, \"successful teardown leaves registered netdev\"); }",
        "int bmc_agent_devm_spi_register_controller(struct device *dev, struct spi_controller *ctlr) { (void)dev; int r = bmc_agent_kernel_status(); if (r == 0) bmc_agent_devm_spi_controller_slot = ctlr; return r; }",
        "int bmc_agent_spi_register_controller(struct spi_controller *ctlr) { int r = bmc_agent_kernel_status(); if (r == 0) bmc_agent_manual_spi_controller_slot = ctlr; return r; }",
        "int bmc_agent_spi_controller_suspend(struct spi_controller *ctlr) { (void)ctlr; int r = bmc_agent_kernel_status(); bmc_agent_spi_suspend_failed = (r != 0); return r; }",
        "void bmc_agent_spi_unregister_controller(struct spi_controller *ctlr) { __CPROVER_assert(ctlr != bmc_agent_devm_spi_controller_slot, \"manual unregister of devm-managed spi controller\"); if (ctlr == bmc_agent_manual_spi_controller_slot) bmc_agent_manual_spi_controller_slot = (void *)0; }",
        "void bmc_agent_spi_devm_dependency_teardown(void) { __CPROVER_assert(bmc_agent_devm_spi_controller_slot == (void *)0, \"teardown before unregister of devm-managed spi controller\"); }",
        "void bmc_agent_spi_suspend_failure_activity(void) { __CPROVER_assert(!bmc_agent_spi_suspend_failed, \"hardware access after failed spi_controller_suspend\"); }",
        "void bmc_agent_spi_suspend_failure_teardown(void) { __CPROVER_assert(!bmc_agent_spi_suspend_failed, \"teardown after failed spi_controller_suspend\"); }",
        "void bmc_agent_i2c_reset_registration_contract(void) { bmc_agent_i2c_adapter_slot = (void *)0; bmc_agent_i2c_registration_failed = 0; }",
        "int bmc_agent_i2c_add_adapter(struct i2c_adapter *adapter) { int r = bmc_agent_kernel_status(); if (r == 0) bmc_agent_i2c_adapter_slot = adapter; else bmc_agent_i2c_registration_failed = 1; return r; }",
        "int bmc_agent_i2c_add_numbered_adapter(struct i2c_adapter *adapter) { return bmc_agent_i2c_add_adapter(adapter); }",
        "void bmc_agent_i2c_del_adapter(struct i2c_adapter *adapter) { if (adapter == bmc_agent_i2c_adapter_slot) bmc_agent_i2c_adapter_slot = (void *)0; }",
        "void bmc_agent_i2c_success_requires_registration(void) { __CPROVER_assert(!bmc_agent_i2c_registration_failed, \"success return after failed i2c_add_adapter\"); }",
        "void bmc_agent_i2c_dependency_teardown(void) { __CPROVER_assert(bmc_agent_i2c_adapter_slot == (void *)0, \"resource teardown before i2c_del_adapter\"); }",
        "void bmc_agent_clk_reset_contract(void) { bmc_agent_clk_slot = (void *)0; bmc_agent_clk_prepare_count = 0; bmc_agent_clk_enable_count = 0; }",
        "int bmc_agent_clk_prepare_enable(struct clk *clk) { int r = bmc_agent_kernel_status(); if (r == 0) { bmc_agent_clk_slot = clk; bmc_agent_clk_prepare_count++; bmc_agent_clk_enable_count++; } return r; }",
        "void bmc_agent_clk_disable(struct clk *clk) { if (clk == bmc_agent_clk_slot) { __CPROVER_assert(bmc_agent_clk_enable_count > 0, \"clk_disable without successful clk_prepare_enable\"); if (bmc_agent_clk_enable_count > 0) bmc_agent_clk_enable_count--; } }",
        "void bmc_agent_clk_unprepare(struct clk *clk) { if (clk == bmc_agent_clk_slot) { __CPROVER_assert(bmc_agent_clk_prepare_count > 0, \"clk_unprepare without successful clk_prepare_enable\"); if (bmc_agent_clk_prepare_count > 0) bmc_agent_clk_prepare_count--; } }",
        "void bmc_agent_clk_balance_disable_unprepare(struct clk *clk) { bmc_agent_clk_disable(clk); bmc_agent_clk_unprepare(clk); }",
        "void bmc_agent_clk_lifecycle_balanced(void) { __CPROVER_assert(bmc_agent_clk_prepare_count == 0, \"successful probe/remove leaves prepared clock\"); __CPROVER_assert(bmc_agent_clk_enable_count == 0, \"successful probe/remove leaves enabled clock\"); }",
        f"int bmc_agent_devm_action_is_regulator_release(void (*action)(void *)) {{ return {devm_regulator_action_condition}; }}",
        "void bmc_agent_devm_regulator_action_release(void) { if (bmc_agent_regulator_enable_count > 0) bmc_agent_regulator_enable_count--; if (bmc_agent_regulator_enable_count == 0) bmc_agent_regulator_slot = (void *)0; }",
        "void bmc_agent_devm_model_action(void (*action)(void *), void *data) { if (!action) return; if (bmc_agent_devm_action_is_regulator_release(action)) bmc_agent_devm_regulator_action_release(); else action(data); }",
        "void bmc_agent_devm_actions_reset(void) { bmc_agent_devm_action_slot = 0; bmc_agent_devm_action_data_slot = (void *)0; bmc_agent_devm_action_registered = 0; }",
        "void bmc_agent_devm_actions_teardown(void) { if (bmc_agent_devm_action_registered && bmc_agent_devm_action_slot) { void (*action)(void *) = bmc_agent_devm_action_slot; void *data = bmc_agent_devm_action_data_slot; bmc_agent_devm_actions_reset(); bmc_agent_devm_model_action(action, data); } }",
        "void bmc_agent_regulator_reset_contract(void) { bmc_agent_devm_actions_reset(); bmc_agent_regulator_known_slot = (void *)0; bmc_agent_regulator_slot = (void *)0; bmc_agent_regulator_enable_count = 0; }",
        "struct regulator *bmc_agent_devm_regulator_get(struct device *dev, const char *id) { (void)dev; (void)id; struct regulator *regulator = (struct regulator *)bmc_agent_ptr_or_err(); if (regulator && !bmc_agent_is_err(regulator)) bmc_agent_regulator_known_slot = regulator; return regulator; }",
        "int bmc_agent_regulator_enable(struct regulator *regulator) { int r = bmc_agent_kernel_status(); if (r == 0 && regulator && !bmc_agent_is_err(regulator)) { bmc_agent_regulator_known_slot = regulator; bmc_agent_regulator_slot = regulator; bmc_agent_regulator_enable_count++; } return r; }",
        "int bmc_agent_regulator_disable(struct regulator *regulator) { if (regulator && !bmc_agent_is_err(regulator) && regulator == bmc_agent_regulator_known_slot) { __CPROVER_assert(bmc_agent_regulator_enable_count > 0, \"regulator_disable without successful regulator_enable\"); if (bmc_agent_regulator_enable_count > 0) bmc_agent_regulator_enable_count--; } return 0; }",
        "void bmc_agent_regulator_lifecycle_balanced(void) { __CPROVER_assert(bmc_agent_regulator_enable_count == 0, \"successful lifecycle leaves enabled regulator\"); }",
        "int bmc_agent_devm_add_action_or_reset(struct device *dev, void (*action)(void *), void *data) { (void)dev; int r = bmc_agent_kernel_status(); if (r != 0) { bmc_agent_devm_model_action(action, data); return r; } bmc_agent_devm_action_slot = action; bmc_agent_devm_action_data_slot = data; bmc_agent_devm_action_registered = (action != 0); return 0; }",
        "int bmc_agent___devm_add_action_or_reset(struct device *dev, void (*action)(void *), void *data, const char *name) { (void)name; return bmc_agent_devm_add_action_or_reset(dev, action, data); }",
        "void bmc_agent_pm_runtime_reset_contract(void) { bmc_agent_pm_runtime_dev_slot = (void *)0; bmc_agent_pm_runtime_enabled = 0; bmc_agent_pm_runtime_autosuspend_enabled = 0; bmc_agent_pm_runtime_usage_count = 0; }",
        "void bmc_agent_pm_runtime_set_active(struct device *dev) { bmc_agent_pm_runtime_dev_slot = dev; }",
        "void bmc_agent_pm_runtime_enable(struct device *dev) { bmc_agent_pm_runtime_dev_slot = dev; bmc_agent_pm_runtime_enabled = 1; }",
        "void bmc_agent_pm_runtime_use_autosuspend(struct device *dev) { bmc_agent_pm_runtime_dev_slot = dev; bmc_agent_pm_runtime_autosuspend_enabled = 1; }",
        "void bmc_agent_pm_runtime_dont_use_autosuspend(struct device *dev) { if (dev == bmc_agent_pm_runtime_dev_slot) bmc_agent_pm_runtime_autosuspend_enabled = 0; }",
        "void bmc_agent_pm_runtime_model_autosuspended(struct device *dev) { if (dev == bmc_agent_pm_runtime_dev_slot && bmc_agent_pm_runtime_autosuspend_enabled) bmc_agent_pm_runtime_usage_count = 0; }",
        "void bmc_agent_pm_runtime_get_noresume(struct device *dev) { bmc_agent_pm_runtime_dev_slot = dev; bmc_agent_pm_runtime_usage_count++; }",
        "int bmc_agent_pm_runtime_get_sync(struct device *dev) { bmc_agent_pm_runtime_dev_slot = dev; bmc_agent_pm_runtime_usage_count++; return bmc_agent_kernel_status(); }",
        "void bmc_agent_pm_runtime_put_noidle(struct device *dev) { if (dev == bmc_agent_pm_runtime_dev_slot) { __CPROVER_assert(bmc_agent_pm_runtime_usage_count > 0, \"pm_runtime_put without matching get\"); if (bmc_agent_pm_runtime_usage_count > 0) bmc_agent_pm_runtime_usage_count--; } }",
        "void bmc_agent_pm_runtime_put_autosuspend(struct device *dev) { bmc_agent_pm_runtime_put_noidle(dev); }",
        "int bmc_agent_pm_runtime_put_sync(struct device *dev) { if (dev == bmc_agent_pm_runtime_dev_slot) { __CPROVER_assert(bmc_agent_pm_runtime_usage_count > 0, \"pm_runtime_put_sync after autosuspend underflow\"); if (bmc_agent_pm_runtime_usage_count > 0) bmc_agent_pm_runtime_usage_count--; } return 0; }",
        "void bmc_agent_pm_runtime_disable(struct device *dev) { if (dev == bmc_agent_pm_runtime_dev_slot) bmc_agent_pm_runtime_enabled = 0; bmc_agent_spi_suspend_failure_teardown(); bmc_agent_spi_devm_dependency_teardown(); }",
        "void bmc_agent_pm_runtime_lifecycle_balanced(void) { __CPROVER_assert(bmc_agent_pm_runtime_usage_count == 0, \"runtime PM usage count left elevated\"); }",
        "void bmc_agent_clk_disable_unprepare(struct clk *clk) { bmc_agent_clk_balance_disable_unprepare(clk); bmc_agent_spi_suspend_failure_teardown(); bmc_agent_spi_devm_dependency_teardown(); }",
        "void bmc_agent_free_irq(unsigned int irq, void *dev_id) { (void)irq; (void)dev_id; bmc_agent_spi_suspend_failure_teardown(); bmc_agent_spi_devm_dependency_teardown(); }",
        "unsigned char bmc_agent_readb(const volatile void *addr) { (void)addr; bmc_agent_spi_suspend_failure_activity(); return (unsigned char)__CPROVER_nondet_uint(); }",
        "unsigned short bmc_agent_readw(const volatile void *addr) { (void)addr; bmc_agent_spi_suspend_failure_activity(); return (unsigned short)__CPROVER_nondet_uint(); }",
        "unsigned int bmc_agent_readl(const volatile void *addr) { (void)addr; bmc_agent_spi_suspend_failure_activity(); return __CPROVER_nondet_uint(); }",
        "unsigned long bmc_agent_readq(const volatile void *addr) { (void)addr; bmc_agent_spi_suspend_failure_activity(); return __CPROVER_nondet_ulong(); }",
        "void bmc_agent_writeb(unsigned char value, volatile void *addr) { (void)value; (void)addr; bmc_agent_spi_suspend_failure_activity(); }",
        "void bmc_agent_writew(unsigned short value, volatile void *addr) { (void)value; (void)addr; bmc_agent_spi_suspend_failure_activity(); }",
        "void bmc_agent_writel(unsigned int value, volatile void *addr) { (void)value; (void)addr; bmc_agent_spi_suspend_failure_activity(); }",
        "void bmc_agent_writeq(unsigned long value, volatile void *addr) { (void)value; (void)addr; bmc_agent_spi_suspend_failure_activity(); }",
        "void bmc_agent_dma_free_coherent(struct device *dev, bmc_agent_kernel_size_t size, void *cpu_addr, unsigned long dma_handle) { (void)dev; (void)size; (void)cpu_addr; (void)dma_handle; bmc_agent_spi_devm_dependency_teardown(); }",
        "void bmc_agent_dma_unmap_single(struct device *dev, unsigned long dma_addr, bmc_agent_kernel_size_t size, int direction) { (void)dev; (void)dma_addr; (void)size; (void)direction; bmc_agent_spi_devm_dependency_teardown(); }",
        "void bmc_agent_dma_unmap_single_attrs(struct device *dev, unsigned long dma_addr, bmc_agent_kernel_size_t size, int direction, unsigned long attrs) { (void)dev; (void)dma_addr; (void)size; (void)direction; (void)attrs; bmc_agent_spi_devm_dependency_teardown(); }",
        "long bmc_agent_strscpy(char *dst, const char *src, bmc_agent_kernel_size_t count) { (void)dst; (void)count; __CPROVER_assert(src != (void *)0, \"string copy source is non-NULL\"); return 0; }",
        "long bmc_agent_sized_strscpy(char *dst, const char *src, bmc_agent_kernel_size_t count) { return bmc_agent_strscpy(dst, src, count); }",
    ]
    existing_helpers = {
        name
        for name in _KERNEL_HELPER_NAMES
        if source_text is not None and _source_defines_function(source_text, name)
    }
    source_text_for_helpers = source_text or ""
    if "ERR_PTR" in external and "ERR_PTR" not in existing_helpers:
        lines.append("void *ERR_PTR(long error) { return bmc_agent_err_ptr(error); }")
    if ("PTR_ERR" in external or "PTR_ERR(" in source_text_for_helpers) and "PTR_ERR" not in existing_helpers:
        lines.append("long PTR_ERR(const void *ptr) { return bmc_agent_ptr_err(ptr); }")
    if ("IS_ERR" in external or "IS_ERR(" in source_text_for_helpers) and "IS_ERR" not in existing_helpers:
        lines.append("int IS_ERR(const void *ptr) { return bmc_agent_is_err(ptr); }")
    if ("IS_ERR_OR_NULL" in external or "IS_ERR_OR_NULL(" in source_text_for_helpers) and "IS_ERR_OR_NULL" not in existing_helpers:
        lines.append("int IS_ERR_OR_NULL(const void *ptr) { return bmc_agent_is_err_or_null(ptr); }")
    if ("PTR_ERR_OR_ZERO" in external or "PTR_ERR_OR_ZERO(" in source_text_for_helpers) and "PTR_ERR_OR_ZERO" not in existing_helpers:
        lines.append("int PTR_ERR_OR_ZERO(const void *ptr) { return bmc_agent_ptr_err_or_zero(ptr); }")
    if "size_add" in external:
        lines.append("bmc_agent_kernel_size_t size_add(bmc_agent_kernel_size_t a, bmc_agent_kernel_size_t b) { return a + b; }")
    if "devm_platform_ioremap_resource" in external:
        lines.append("void *devm_platform_ioremap_resource(struct platform_device *pdev, unsigned int index) { (void)pdev; (void)index; return bmc_agent_ptr_or_err(); }")
    if "devm_clk_get" in external:
        lines.append("struct clk *devm_clk_get(struct device *dev, const char *id) { (void)dev; (void)id; return (struct clk *)bmc_agent_ptr_or_err(); }")
    if "devm_regulator_get" in external:
        lines.append("struct regulator *devm_regulator_get(struct device *dev, const char *id) { return bmc_agent_devm_regulator_get(dev, id); }")
    if "devm_regulator_get_optional" in external:
        lines.append("struct regulator *devm_regulator_get_optional(struct device *dev, const char *id) { return bmc_agent_devm_regulator_get(dev, id); }")
    if "devm_gpiod_get_optional" in external:
        lines.append("struct gpio_desc *devm_gpiod_get_optional(struct device *dev, const char *con_id, int flags) { (void)dev; (void)con_id; (void)flags; return (struct gpio_desc *)bmc_agent_ptr_or_err(); }")
    if "devm_kasprintf" in external:
        lines.append("char *devm_kasprintf(struct device *dev, unsigned int gfp, const char *fmt, ...) { (void)dev; (void)gfp; (void)fmt; if (__CPROVER_nondet_bool()) return (char *)0; return (char *)bmc_agent_valid_alloc(64); }")
    if "dev_err_probe" in external:
        lines.append("int dev_err_probe(const struct device *dev, int err, const char *fmt, ...) { (void)dev; (void)fmt; return err; }")
    if "usb_control_msg" in external:
        lines.append("int usb_control_msg(struct usb_device *dev, unsigned int pipe, unsigned char request, unsigned char requesttype, unsigned short value, unsigned short index, void *data, unsigned short size, int timeout) { (void)dev; (void)pipe; (void)request; (void)requesttype; (void)value; (void)index; (void)data; (void)timeout; if (__CPROVER_nondet_bool()) return (int)size; return bmc_agent_kernel_status(); }")
    status_stubs = {
        "devm_of_clk_add_hw_provider": "int devm_of_clk_add_hw_provider(struct device *dev, struct clk_hw *(*get)(struct of_phandle_args *, void *), void *data) { (void)dev; (void)get; (void)data; return bmc_agent_kernel_status(); }",
        "devm_clk_hw_register": "int devm_clk_hw_register(struct device *dev, struct clk_hw *hw) { (void)dev; (void)hw; return bmc_agent_kernel_status(); }",
        "devm_request_irq": "int devm_request_irq(struct device *dev, unsigned int irq, int (*handler)(int, void *), unsigned long flags, const char *name, void *dev_id) { (void)dev; (void)irq; (void)handler; (void)flags; (void)name; (void)dev_id; return bmc_agent_kernel_status(); }",
        "i3c_master_register": "int i3c_master_register(struct i3c_master_controller *master, struct device *parent, const struct i3c_master_controller_ops *ops, _Bool secondary) { (void)master; (void)parent; (void)ops; (void)secondary; return bmc_agent_kernel_status(); }",
        "i2c_add_adapter": "int i2c_add_adapter(struct i2c_adapter *adapter) { return bmc_agent_i2c_add_adapter(adapter); }",
        "i2c_add_numbered_adapter": "int i2c_add_numbered_adapter(struct i2c_adapter *adapter) { return bmc_agent_i2c_add_numbered_adapter(adapter); }",
        "usb_set_interface": "int usb_set_interface(struct usb_device *udev, int interface, int alternate) { (void)udev; (void)interface; (void)alternate; return bmc_agent_kernel_status(); }",
        "usb_register_dev": "int usb_register_dev(struct usb_interface *intf, const struct usb_class_driver *class_driver) { return bmc_agent_usb_register_dev(intf, class_driver); }",
        "usb_submit_urb": "int usb_submit_urb(struct urb *urb, unsigned int mem_flags) { return bmc_agent_usb_submit_urb(urb, mem_flags); }",
        "register_netdev": "int register_netdev(struct net_device *dev) { return bmc_agent_register_netdev(dev); }",
        "regulator_enable": "int regulator_enable(struct regulator *regulator) { return bmc_agent_regulator_enable(regulator); }",
        "regulator_disable": "int regulator_disable(struct regulator *regulator) { return bmc_agent_regulator_disable(regulator); }",
        "devm_add_action_or_reset": "int devm_add_action_or_reset(struct device *dev, void (*action)(void *), void *data) { return bmc_agent_devm_add_action_or_reset(dev, action, data); }",
        "__devm_add_action_or_reset": "int __devm_add_action_or_reset(struct device *dev, void (*action)(void *), void *data, const char *name) { return bmc_agent___devm_add_action_or_reset(dev, action, data, name); }",
        "platform_device_add": "int platform_device_add(struct platform_device *pdev) { (void)pdev; return bmc_agent_kernel_status(); }",
        "platform_device_add_data": "int platform_device_add_data(struct platform_device *pdev, const void *data, bmc_agent_kernel_size_t size) { (void)pdev; (void)data; (void)size; return bmc_agent_kernel_status(); }",
        "pci_enable_device": "int pci_enable_device(struct pci_dev *dev) { (void)dev; return bmc_agent_kernel_status(); }",
        "pci_request_regions": "int pci_request_regions(struct pci_dev *pdev, const char *res_name) { (void)pdev; (void)res_name; return bmc_agent_kernel_status(); }",
        "request_irq": "int request_irq(unsigned int irq, int (*handler)(int, void *), unsigned long flags, const char *name, void *dev) { (void)irq; (void)handler; (void)flags; (void)name; (void)dev; return bmc_agent_kernel_status(); }",
        "timer_shutdown_sync": "int timer_shutdown_sync(struct timer_list *timer) { (void)timer; return 0; }",
        "pm_runtime_get_sync": "int pm_runtime_get_sync(struct device *dev) { return bmc_agent_pm_runtime_get_sync(dev); }",
        "pm_runtime_put_sync": "int pm_runtime_put_sync(struct device *dev) { return bmc_agent_pm_runtime_put_sync(dev); }",
        "platform_get_irq": "int platform_get_irq(struct platform_device *pdev, unsigned int num) { (void)pdev; (void)num; return bmc_agent_kernel_status(); }",
        "device_property_read_u32": "int device_property_read_u32(struct device *dev, const char *propname, unsigned int *val) { (void)dev; (void)propname; if (val) *val = __CPROVER_nondet_uint(); return bmc_agent_kernel_status(); }",
        "of_property_read_bool": "_Bool of_property_read_bool(const struct device_node *np, const char *propname) { (void)np; (void)propname; return __CPROVER_nondet_bool(); }",
        "dma_mapping_error": "int dma_mapping_error(struct device *dev, unsigned long dma_addr) { (void)dev; (void)dma_addr; return __CPROVER_nondet_bool(); }",
        "input_ff_create_memless": "int input_ff_create_memless(struct input_dev *dev, void *data, int (*play_effect)(struct input_dev *, void *, struct ff_effect *)) { (void)dev; (void)data; (void)play_effect; return bmc_agent_kernel_status(); }",
        "input_register_device": "int input_register_device(struct input_dev *dev) { (void)dev; return bmc_agent_kernel_status(); }",
        "regmap_update_bits": "int regmap_update_bits(struct regmap *map, unsigned int reg, unsigned int mask, unsigned int val) { (void)map; (void)reg; (void)mask; (void)val; return bmc_agent_kernel_status(); }",
        "class_mutex_constructor": "void *class_mutex_constructor(void *lock) { return lock; }",
        "input_device_enabled": "_Bool input_device_enabled(struct input_dev *dev) { (void)dev; return __CPROVER_nondet_bool(); }",
        "cancel_work_sync": "_Bool cancel_work_sync(struct work_struct *work) { (void)work; return __CPROVER_nondet_bool(); }",
    }
    for name, stub in status_stubs.items():
        if name in external:
            lines.append(stub)
    void_stubs = {
        "platform_set_drvdata": "void platform_set_drvdata(struct platform_device *pdev, void *data) { bmc_agent_platform_set_drvdata(pdev, data); }",
        "dev_set_drvdata": "void dev_set_drvdata(struct device *dev, void *data) { bmc_agent_dev_set_drvdata(dev, data); }",
        "i2c_set_clientdata": "void i2c_set_clientdata(struct i2c_client *client, void *data) { bmc_agent_i2c_set_clientdata((void *)client, data); }",
        "usb_set_intfdata": "void usb_set_intfdata(struct usb_interface *intf, void *data) { bmc_agent_usb_set_intfdata(intf, data); }",
        "usb_deregister_dev": "void usb_deregister_dev(struct usb_interface *intf, const struct usb_class_driver *class_driver) { bmc_agent_usb_deregister_dev(intf, class_driver); }",
        "unregister_netdev": "void unregister_netdev(struct net_device *dev) { bmc_agent_unregister_netdev(dev); }",
        "free_netdev": "void free_netdev(struct net_device *dev) { bmc_agent_free_netdev(dev); }",
        "netif_napi_add": "void netif_napi_add(struct net_device *dev, struct napi_struct *napi, int (*poll)(struct napi_struct *, int)) { bmc_agent_netif_napi_add(dev, napi, poll); }",
        "netif_napi_add_weight": "void netif_napi_add_weight(struct net_device *dev, struct napi_struct *napi, int (*poll)(struct napi_struct *, int), int weight) { bmc_agent_netif_napi_add_weight(dev, napi, poll, weight); }",
        "netif_napi_del": "void netif_napi_del(struct napi_struct *napi) { bmc_agent_netif_napi_del(napi); }",
        "iio_device_set_drvdata": "void iio_device_set_drvdata(struct iio_dev *indio_dev, void *data) { (void)indio_dev; (void)data; }",
        "i2c_del_adapter": "void i2c_del_adapter(struct i2c_adapter *adapter) { bmc_agent_i2c_del_adapter(adapter); }",
        "pm_runtime_dont_use_autosuspend": "void pm_runtime_dont_use_autosuspend(struct device *dev) { bmc_agent_pm_runtime_dont_use_autosuspend(dev); }",
        "pm_runtime_enable": "void pm_runtime_enable(struct device *dev) { bmc_agent_pm_runtime_enable(dev); }",
        "pm_runtime_get_noresume": "void pm_runtime_get_noresume(struct device *dev) { bmc_agent_pm_runtime_get_noresume(dev); }",
        "pm_runtime_put_autosuspend": "void pm_runtime_put_autosuspend(struct device *dev) { bmc_agent_pm_runtime_put_autosuspend(dev); }",
        "pm_runtime_put_noidle": "void pm_runtime_put_noidle(struct device *dev) { bmc_agent_pm_runtime_put_noidle(dev); }",
        "pm_runtime_set_active": "void pm_runtime_set_active(struct device *dev) { bmc_agent_pm_runtime_set_active(dev); }",
        "pm_runtime_set_autosuspend_delay": "void pm_runtime_set_autosuspend_delay(struct device *dev, int delay) { (void)dev; (void)delay; }",
        "pm_runtime_mark_last_busy": "void pm_runtime_mark_last_busy(struct device *dev) { (void)dev; }",
        "pm_runtime_set_suspended": "void pm_runtime_set_suspended(struct device *dev) { (void)dev; }",
        "pm_runtime_use_autosuspend": "void pm_runtime_use_autosuspend(struct device *dev) { bmc_agent_pm_runtime_use_autosuspend(dev); }",
        "device_set_of_node_from_dev": "void device_set_of_node_from_dev(struct device *dev, const struct device *from) { (void)dev; (void)from; }",
        "i3c_master_unregister": "void i3c_master_unregister(struct i3c_master_controller *master) { (void)master; }",
        "pci_disable_device": "void pci_disable_device(struct pci_dev *dev) { (void)dev; }",
        "pci_release_regions": "void pci_release_regions(struct pci_dev *pdev) { (void)pdev; }",
        "msleep": "void msleep(unsigned int msecs) { (void)msecs; }",
        "usleep_range": "void usleep_range(unsigned long min, unsigned long max) { (void)min; (void)max; }",
        "enable_irq": "void enable_irq(unsigned int irq) { (void)irq; }",
        "disable_irq": "void disable_irq(unsigned int irq) { (void)irq; }",
        "gpiod_set_value_cansleep": "void gpiod_set_value_cansleep(struct gpio_desc *desc, int value) { (void)desc; (void)value; }",
        "usb_free_urb": "void usb_free_urb(struct urb *urb) { bmc_agent_usb_free_urb(urb); }",
        "usb_kill_urb": "void usb_kill_urb(struct urb *urb) { bmc_agent_usb_kill_urb(urb); }",
        "usb_scuttle_anchored_urbs": "void usb_scuttle_anchored_urbs(struct usb_anchor *anchor) { (void)anchor; }",
        "phylink_stop": "void phylink_stop(struct phylink *pl) { (void)pl; }",
        "phylink_disconnect_phy": "void phylink_disconnect_phy(struct phylink *pl) { (void)pl; }",
        "phylink_destroy": "void phylink_destroy(struct phylink *pl) { (void)pl; }",
        "_dev_err": "void _dev_err(const struct device *dev, const char *fmt, ...) { (void)dev; (void)fmt; }",
        "input_set_drvdata": "void input_set_drvdata(struct input_dev *dev, void *data) { (void)dev; (void)data; }",
        "input_set_capability": "void input_set_capability(struct input_dev *dev, unsigned int type, unsigned int code) { (void)dev; (void)type; (void)code; }",
        "__init_work": "void __init_work(struct work_struct *work, int onstack) { (void)work; (void)onstack; }",
        "INIT_LIST_HEAD": "void INIT_LIST_HEAD(struct list_head *list) { (void)list; }",
        "lockdep_init_map": "void lockdep_init_map(struct lockdep_map *lock, const char *name, struct lock_class_key *key, int subclass) { (void)lock; (void)name; (void)key; (void)subclass; }",
        "gpiod_set_value": "int gpiod_set_value(struct gpio_desc *desc, int value) { (void)desc; (void)value; return 0; }",
    }
    for name, stub in void_stubs.items():
        if name in external:
            lines.append(stub)
    pointer_stubs = {
        "platform_get_drvdata": "void *platform_get_drvdata(const struct platform_device *pdev) { return bmc_agent_platform_get_drvdata(pdev); }",
        "dev_get_drvdata": "void *dev_get_drvdata(const struct device *dev) { return bmc_agent_dev_get_drvdata(dev); }",
        "usb_get_intfdata": "void *usb_get_intfdata(struct usb_interface *intf) { return bmc_agent_usb_get_intfdata(intf); }",
        "iio_device_get_drvdata": "void *iio_device_get_drvdata(struct iio_dev *indio_dev) { (void)indio_dev; return bmc_agent_valid_alloc(64); }",
        "spi_controller_get_devdata": "void *spi_controller_get_devdata(struct spi_controller *ctlr) { (void)ctlr; return bmc_agent_valid_alloc(64); }",
        "dev_get_platdata": "void *dev_get_platdata(const struct device *dev) { (void)dev; return bmc_agent_valid_alloc(64); }",
        "pci_get_drvdata": "void *pci_get_drvdata(struct pci_dev *pdev) { (void)pdev; return bmc_agent_valid_alloc(64); }",
        "devm_spi_alloc_host": "void *devm_spi_alloc_host(struct device *dev, unsigned int size) { (void)dev; (void)size; return bmc_agent_valid_alloc(4096); }",
        "spi_alloc_host": "void *spi_alloc_host(struct device *dev, unsigned int size) { (void)dev; (void)size; return bmc_agent_valid_alloc(4096); }",
        "spi_controller_get": "void *spi_controller_get(struct spi_controller *ctlr) { (void)ctlr; return bmc_agent_valid_alloc(64); }",
        "platform_device_alloc": "struct platform_device *platform_device_alloc(const char *name, int id) { (void)name; (void)id; return (struct platform_device *)bmc_agent_valid_alloc(256); }",
        "pci_iomap": "void *pci_iomap(struct pci_dev *dev, int bar, unsigned long maxlen) { (void)dev; (void)bar; (void)maxlen; return bmc_agent_valid_alloc(4096); }",
        "dma_alloc_coherent": "void *dma_alloc_coherent(struct device *dev, bmc_agent_kernel_size_t size, unsigned long *dma_handle, unsigned int flag) { (void)dev; (void)flag; if (dma_handle) *dma_handle = __CPROVER_nondet_ulong(); return bmc_agent_valid_alloc(size); }",
        "get_free_pages_noprof": "unsigned long get_free_pages_noprof(unsigned int gfp_mask, unsigned int order) { (void)gfp_mask; (void)order; return (unsigned long)bmc_agent_valid_alloc(4096); }",
        "alloc_etherdev_mqs": "struct net_device *alloc_etherdev_mqs(int sizeof_priv, unsigned int txqs, unsigned int rxqs) { (void)sizeof_priv; (void)txqs; (void)rxqs; return (struct net_device *)bmc_agent_valid_alloc(65536); }",
        "netdev_priv": "void *netdev_priv(const struct net_device *dev) { (void)dev; return bmc_agent_valid_alloc(65536); }",
        "usb_alloc_urb": "struct urb *usb_alloc_urb(int iso_packets, unsigned int mem_flags) { return bmc_agent_usb_alloc_urb(iso_packets, mem_flags); }",
        "device_get_match_data": "const void *device_get_match_data(const struct device *dev) { (void)dev; return bmc_agent_valid_alloc(64); }",
        "devm_input_allocate_device": "struct input_dev *devm_input_allocate_device(struct device *dev) { (void)dev; return (struct input_dev *)bmc_agent_valid_alloc(4096); }",
        "__devm_regmap_init_i2c": "struct regmap *__devm_regmap_init_i2c(struct i2c_client *client, const struct regmap_config *config, struct lock_class_key *lock_key, const char *lock_name) { (void)client; (void)config; (void)lock_key; (void)lock_name; return (struct regmap *)bmc_agent_ptr_or_err(); }",
        "devm_regmap_init_i2c": "struct regmap *devm_regmap_init_i2c(struct i2c_client *client, const struct regmap_config *config) { (void)client; (void)config; return (struct regmap *)bmc_agent_ptr_or_err(); }",
        "devm_clk_get_enabled": "struct clk *devm_clk_get_enabled(struct device *dev, const char *id) { (void)dev; (void)id; return (struct clk *)bmc_agent_valid_alloc(64); }",
        "devm_clk_get_optional_enabled": "struct clk *devm_clk_get_optional_enabled(struct device *dev, const char *id) { (void)dev; (void)id; return (struct clk *)bmc_agent_valid_alloc(64); }",
        "devm_reset_control_get_optional_exclusive_deasserted": "struct reset_control *devm_reset_control_get_optional_exclusive_deasserted(struct device *dev, const char *id) { (void)dev; (void)id; return (struct reset_control *)bmc_agent_valid_alloc(64); }",
    }
    for name, stub in pointer_stubs.items():
        if name in external:
            lines.append(stub)
    # Registration APIs often pass this as a function pointer, so it may not
    # appear in the call graph's external-symbol list.
    lines.append("struct clk_hw *of_clk_hw_onecell_get(struct of_phandle_args *clkspec, void *data) { (void)clkspec; (void)data; return (struct clk_hw *)bmc_agent_valid_alloc(64); }")
    if "readl" in external:
        lines.append("unsigned int readl(const volatile void *addr) { return bmc_agent_readl(addr); }")
    if "writel" in external:
        lines.append("void writel(unsigned int value, volatile void *addr) { bmc_agent_writel(value, addr); }")
    if "get_unaligned_be16" in external:
        lines.append("unsigned short get_unaligned_be16(const void *p) { (void)p; unsigned short r; return r; }")
    if "get_unaligned_be32" in external:
        lines.append("unsigned int get_unaligned_be32(const void *p) { (void)p; return __CPROVER_nondet_uint(); }")
    if "sign_extend32" in external:
        lines.append("int sign_extend32(unsigned int value, int index) { (void)index; return (int)value; }")
    if "strchrnul" in external:
        lines.append("char *strchrnul(const char *s, int c) { (void)c; return (char *)s; }")
    lines.extend([
        "#endif",
        "",
    ])
    return "\n".join(lines)


_KERNEL_ATOMIC_TYPEDEF_RE = re.compile(
    r"typedef\s+struct\s*\{\s*"
    r"int\s+__attribute__\s*\(\(\s*__aligned__\s*\(\s*sizeof\s*\(\s*int\s*\)\s*\)\s*\)\)\s+counter\s*;"
    r"\s*\}\s*atomic_t\s*;",
    re.S,
)
_KERNEL_S128_TYPEDEF_RE = re.compile(
    r"typedef\s+__signed__\s+__int128\s+__s128\s+__attribute__\s*\(\(\s*aligned\s*\(\s*16\s*\)\s*\)\)\s*;"
)
_KERNEL_U128_TYPEDEF_RE = re.compile(
    r"typedef\s+unsigned\s+__int128\s+__u128\s+__attribute__\s*\(\(\s*aligned\s*\(\s*16\s*\)\s*\)\)\s*;"
)
def simplify_kernel_compat_typedefs(source: str) -> str:
    """Normalize kernel typedefs that CBMC commonly fails to parse."""
    source = _KERNEL_S128_TYPEDEF_RE.sub("typedef signed long long __s128;", source)
    source = _KERNEL_U128_TYPEDEF_RE.sub("typedef unsigned long long __u128;", source)
    return _KERNEL_ATOMIC_TYPEDEF_RE.sub(
        "typedef struct { int counter; } atomic_t;",
        source,
    )


def materialize_kernel_mode_source(
    pkg: PackageInfo,
    discovery: Discovery,
    output: Path,
    *,
    harnesses: list[GeneratedHarness] | None = None,
    devm_regulator_callbacks: set[str] | None = None,
) -> Path:
    """Write a run-local source_i copy with a kernel environment prelude."""
    run_source = output / "kernel_mode_source.i"
    original = pkg.source_i.read_text(encoding="utf-8", errors="replace")
    prelude = kernel_env_prelude(
        discovery,
        source_text=original,
        devm_regulator_callbacks=devm_regulator_callbacks,
    )
    original = insert_kernel_contract_macros(original, pkg)
    original = simplify_kernel_compat_typedefs(original)
    generated = "\n".join(h.source for h in (harnesses or []))
    run_source.write_text(prelude + "\n" + original + "\n" + generated, encoding="utf-8")
    return run_source


def kernel_contract_macro_block() -> str:
    return "\n".join(
        [
            "",
            "/* BMC-Agent kernel driver-mode contract remapping.",
            " * Inserted after kernel inline definitions and before driver code.",
            " */",
            "#define IS_ERR(ptr) bmc_agent_is_err((ptr))",
            "#define IS_ERR_OR_NULL(ptr) bmc_agent_is_err_or_null((ptr))",
            "#define PTR_ERR(ptr) bmc_agent_ptr_err((ptr))",
            "#define PTR_ERR_OR_ZERO(ptr) bmc_agent_ptr_err_or_zero((ptr))",
            "#define platform_set_drvdata(pdev, data) bmc_agent_platform_set_drvdata((pdev), (data))",
            "#define platform_get_drvdata(pdev) bmc_agent_platform_get_drvdata((pdev))",
            "#define dev_set_drvdata(dev, data) bmc_agent_dev_set_drvdata((dev), (data))",
            "#define dev_get_drvdata(dev) bmc_agent_dev_get_drvdata((dev))",
            "#define i2c_set_clientdata(client, data) bmc_agent_i2c_set_clientdata((client), (data))",
            "#define usb_set_intfdata(intf, data) bmc_agent_usb_set_intfdata((intf), (data))",
            "#define usb_get_intfdata(intf) bmc_agent_usb_get_intfdata((intf))",
            "#define usb_register_dev(intf, class_driver) bmc_agent_usb_register_dev((intf), (class_driver))",
            "#define usb_deregister_dev(intf, class_driver) bmc_agent_usb_deregister_dev((intf), (class_driver))",
            "#define usb_alloc_urb(iso_packets, mem_flags) bmc_agent_usb_alloc_urb((iso_packets), (mem_flags))",
            "#define usb_submit_urb(urb, mem_flags) bmc_agent_usb_submit_urb((urb), (mem_flags))",
            "#define usb_free_urb(urb) bmc_agent_usb_free_urb((urb))",
            "#define usb_kill_urb(urb) bmc_agent_usb_kill_urb((urb))",
            "#define register_netdev(dev) bmc_agent_register_netdev((dev))",
            "#define unregister_netdev(dev) bmc_agent_unregister_netdev((dev))",
            "#define free_netdev(dev) bmc_agent_free_netdev((dev))",
            "#define netif_napi_add(dev, napi, poll) bmc_agent_netif_napi_add((dev), (napi), (poll))",
            "#define netif_napi_add_weight(dev, napi, poll, weight) bmc_agent_netif_napi_add_weight((dev), (napi), (poll), (weight))",
            "#define netif_napi_del(napi) bmc_agent_netif_napi_del((napi))",
            "#define devm_spi_register_controller(dev, ctlr) bmc_agent_devm_spi_register_controller((dev), (ctlr))",
            "#define spi_register_controller(ctlr) bmc_agent_spi_register_controller((ctlr))",
            "#define spi_controller_suspend(ctlr) bmc_agent_spi_controller_suspend((ctlr))",
            "#define spi_unregister_controller(ctlr) bmc_agent_spi_unregister_controller((ctlr))",
            "#define i2c_add_adapter(adapter) bmc_agent_i2c_add_adapter((adapter))",
            "#define i2c_add_numbered_adapter(adapter) bmc_agent_i2c_add_numbered_adapter((adapter))",
            "#define i2c_del_adapter(adapter) bmc_agent_i2c_del_adapter((adapter))",
            "#define clk_prepare_enable(clk) bmc_agent_clk_prepare_enable((clk))",
            "#define clk_disable(clk) bmc_agent_clk_disable((clk))",
            "#define clk_unprepare(clk) bmc_agent_clk_unprepare((clk))",
            "#define regulator_enable(regulator) bmc_agent_regulator_enable((regulator))",
            "#define regulator_disable(regulator) bmc_agent_regulator_disable((regulator))",
            "#define devm_add_action_or_reset(dev, action, data) bmc_agent_devm_add_action_or_reset((dev), (action), (data))",
            "#define __devm_add_action_or_reset(dev, action, data, name) bmc_agent___devm_add_action_or_reset((dev), (action), (data), (name))",
            "#define pm_runtime_disable(dev) bmc_agent_pm_runtime_disable((dev))",
            "#define pm_runtime_enable(dev) bmc_agent_pm_runtime_enable((dev))",
            "#define pm_runtime_get_noresume(dev) bmc_agent_pm_runtime_get_noresume((dev))",
            "#define pm_runtime_get_sync(dev) bmc_agent_pm_runtime_get_sync((dev))",
            "#define pm_runtime_put_sync(dev) bmc_agent_pm_runtime_put_sync((dev))",
            "#define pm_runtime_put_noidle(dev) bmc_agent_pm_runtime_put_noidle((dev))",
            "#define pm_runtime_put_autosuspend(dev) bmc_agent_pm_runtime_put_autosuspend((dev))",
            "#define pm_runtime_set_active(dev) bmc_agent_pm_runtime_set_active((dev))",
            "#define pm_runtime_use_autosuspend(dev) bmc_agent_pm_runtime_use_autosuspend((dev))",
            "#define pm_runtime_dont_use_autosuspend(dev) bmc_agent_pm_runtime_dont_use_autosuspend((dev))",
            "#define clk_disable_unprepare(clk) bmc_agent_clk_disable_unprepare((clk))",
            "#define free_irq(irq, dev_id) bmc_agent_free_irq((irq), (dev_id))",
            "#define readb(addr) bmc_agent_readb((addr))",
            "#define readw(addr) bmc_agent_readw((addr))",
            "#define readl(addr) bmc_agent_readl((addr))",
            "#define readq(addr) bmc_agent_readq((addr))",
            "#define writeb(value, addr) bmc_agent_writeb((value), (addr))",
            "#define writew(value, addr) bmc_agent_writew((value), (addr))",
            "#define writel(value, addr) bmc_agent_writel((value), (addr))",
            "#define writeq(value, addr) bmc_agent_writeq((value), (addr))",
            "#define dma_free_coherent(dev, size, cpu_addr, dma_handle) bmc_agent_dma_free_coherent((dev), (size), (cpu_addr), (dma_handle))",
            "#define dma_unmap_single(dev, dma_addr, size, dir) bmc_agent_dma_unmap_single((dev), (dma_addr), (size), (dir))",
            "#define dma_unmap_single_attrs(dev, dma_addr, size, dir, attrs) bmc_agent_dma_unmap_single_attrs((dev), (dma_addr), (size), (dir), (attrs))",
            "#define strscpy(dst, src, count) bmc_agent_strscpy((dst), (src), (count))",
            "#define sized_strscpy(dst, src, count) bmc_agent_sized_strscpy((dst), (src), (count))",
            "#define kfree(ptr) bmc_agent_kfree((ptr))",
            "",
        ]
    )


_KERNEL_CONTRACT_CALL_REWRITES = {
    "platform_set_drvdata": "bmc_agent_platform_set_drvdata",
    "platform_get_drvdata": "bmc_agent_platform_get_drvdata",
    "dev_set_drvdata": "bmc_agent_dev_set_drvdata",
    "dev_get_drvdata": "bmc_agent_dev_get_drvdata",
    "i2c_set_clientdata": "bmc_agent_i2c_set_clientdata",
    "usb_set_intfdata": "bmc_agent_usb_set_intfdata",
    "usb_get_intfdata": "bmc_agent_usb_get_intfdata",
    "usb_register_dev": "bmc_agent_usb_register_dev",
    "usb_deregister_dev": "bmc_agent_usb_deregister_dev",
    "usb_alloc_urb": "bmc_agent_usb_alloc_urb",
    "usb_submit_urb": "bmc_agent_usb_submit_urb",
    "usb_free_urb": "bmc_agent_usb_free_urb",
    "usb_kill_urb": "bmc_agent_usb_kill_urb",
    "register_netdev": "bmc_agent_register_netdev",
    "unregister_netdev": "bmc_agent_unregister_netdev",
    "free_netdev": "bmc_agent_free_netdev",
    "netif_napi_add": "bmc_agent_netif_napi_add",
    "netif_napi_add_weight": "bmc_agent_netif_napi_add_weight",
    "netif_napi_del": "bmc_agent_netif_napi_del",
    "devm_spi_register_controller": "bmc_agent_devm_spi_register_controller",
    "spi_register_controller": "bmc_agent_spi_register_controller",
    "spi_controller_suspend": "bmc_agent_spi_controller_suspend",
    "spi_unregister_controller": "bmc_agent_spi_unregister_controller",
    "i2c_add_adapter": "bmc_agent_i2c_add_adapter",
    "i2c_add_numbered_adapter": "bmc_agent_i2c_add_numbered_adapter",
    "i2c_del_adapter": "bmc_agent_i2c_del_adapter",
    "clk_prepare_enable": "bmc_agent_clk_prepare_enable",
    "clk_disable_unprepare": "bmc_agent_clk_disable_unprepare",
    "clk_disable": "bmc_agent_clk_disable",
    "clk_unprepare": "bmc_agent_clk_unprepare",
    "regulator_enable": "bmc_agent_regulator_enable",
    "regulator_disable": "bmc_agent_regulator_disable",
    "devm_add_action_or_reset": "bmc_agent_devm_add_action_or_reset",
    "__devm_add_action_or_reset": "bmc_agent___devm_add_action_or_reset",
    "pm_runtime_disable": "bmc_agent_pm_runtime_disable",
    "pm_runtime_enable": "bmc_agent_pm_runtime_enable",
    "pm_runtime_get_noresume": "bmc_agent_pm_runtime_get_noresume",
    "pm_runtime_get_sync": "bmc_agent_pm_runtime_get_sync",
    "pm_runtime_put_sync": "bmc_agent_pm_runtime_put_sync",
    "pm_runtime_put_noidle": "bmc_agent_pm_runtime_put_noidle",
    "pm_runtime_put_autosuspend": "bmc_agent_pm_runtime_put_autosuspend",
    "pm_runtime_set_active": "bmc_agent_pm_runtime_set_active",
    "pm_runtime_use_autosuspend": "bmc_agent_pm_runtime_use_autosuspend",
    "pm_runtime_dont_use_autosuspend": "bmc_agent_pm_runtime_dont_use_autosuspend",
    "free_irq": "bmc_agent_free_irq",
    "readb": "bmc_agent_readb",
    "readw": "bmc_agent_readw",
    "readl": "bmc_agent_readl",
    "readq": "bmc_agent_readq",
    "writeb": "bmc_agent_writeb",
    "writew": "bmc_agent_writew",
    "writel": "bmc_agent_writel",
    "writeq": "bmc_agent_writeq",
    "dma_free_coherent": "bmc_agent_dma_free_coherent",
    "dma_unmap_single": "bmc_agent_dma_unmap_single",
    "dma_unmap_single_attrs": "bmc_agent_dma_unmap_single_attrs",
    "strscpy": "bmc_agent_strscpy",
    "sized_strscpy": "bmc_agent_sized_strscpy",
    "kfree": "bmc_agent_kfree",
}


def rewrite_kernel_contract_calls(text: str) -> str:
    """Apply contract remapping to driver-region source text.

    The BMC-Agent parser is text based and does not expand C preprocessor
    macros before extracting inline callee bodies. Keep the macro block for
    CBMC, but also rewrite the primary driver-region call text so generated
    Phase-3 harnesses preserve the same kernel API contract semantics.
    """
    for src, dst in sorted(
        _KERNEL_CONTRACT_CALL_REWRITES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        text = re.sub(rf"\b{re.escape(src)}\s*\(", f"{dst}(", text)
    return text


def insert_kernel_contract_macros(original: str, pkg: PackageInfo) -> str:
    source_rel = (
        (pkg.provenance.get("analysis_context") or {}).get("source_rel")
        or (pkg.provenance.get("case") or {}).get("source_rel")
    )
    if not source_rel:
        return original
    pattern = re.compile(rf'^#\s+\d+\s+"{re.escape(str(source_rel))}"(?:\s+\d+)?$', re.MULTILINE)
    matches = list(pattern.finditer(original))
    if not matches:
        return original
    # In kernel .i files, early source-file line directives cover includes.
    # Use the last resume point for the primary driver source so kernel
    # declarations are already parsed and only the driver implementation calls
    # are remapped.
    candidates = [m for m in matches if m.start() > len(original) // 2]
    marker = candidates[-1] if candidates else matches[-1]
    insert_at = original.find("\n", marker.end())
    if insert_at < 0:
        insert_at = marker.end()
    else:
        insert_at += 1
    driver_region = rewrite_kernel_contract_calls(original[insert_at:])
    return original[:insert_at] + kernel_contract_macro_block() + driver_region


def run_bmc(discovery: Discovery, args: argparse.Namespace) -> int:
    from bmc_agent.config import Config
    from bmc_agent.pipeline import AMCPipeline

    selected_entries = select_entrypoints(
        discovery.entrypoints,
        policy=args.entrypoint_policy,
        max_entrypoints=args.max_entrypoints,
    )
    entrypoints = [entry.name for entry in selected_entries]
    if not entrypoints:
        raise SystemExit("no entrypoints discovered; run discover first and inspect warnings")

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(discovery)
    (output / "kernel_mode_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        print("Would verify entrypoints: " + ", ".join(entrypoints), file=sys.stderr)
        return 0

    config = Config.from_env()
    config.artifact_dir = str(output)
    if args.minimal:
        config.lite_mode = True
        config.enable_realism_check = False
        config.enable_dynamic_validation = False
        config.enable_flag_selection = False
        config.enable_feedback_loop = False
        config.enable_spec_refiner = False
        config.enable_inlining_advisor = False
        config.enable_spec_gen_tools = False
        config.enable_realism_tools = False
        config.skip_refinement = True
        config.disable_llm_fallbacks = True
    elif args.lite_mode:
        config.lite_mode = True
        config.enable_flag_selection = False
        config.enable_feedback_loop = False
        config.enable_spec_refiner = False
        config.enable_inlining_advisor = False
        config.enable_spec_gen_tools = False
        config.enable_realism_tools = False
        config.enable_adjacent_bug_discovery = False
    if args.no_dynamic_validation:
        config.enable_dynamic_validation = False
    if args.no_realism_check:
        config.enable_realism_check = False
    phase3_allowlist = _parse_csv(getattr(args, "phase3_property_allowlist", ""))
    if phase3_allowlist:
        config.phase3_property_allowlist = tuple(phase3_allowlist)
    if args.threat_model:
        config.threat_model = args.threat_model

    pkg = load_package(Path(discovery.package))
    parsed_context, _context_text = parse_driver_context(pkg.analysis_source)
    devm_action_callbacks = devm_action_callback_names(
        _context_text,
        set(discovery.driver_functions),
    )
    devm_regulator_callbacks = devm_regulator_action_callback_names(
        _context_text,
        set(discovery.driver_functions),
    )
    generated_harnesses = (
        generated_lifecycle_harnesses(discovery, parsed_context)
        if args.lifecycle_harness
        else []
    )
    harness_names = [h.name for h in generated_harnesses]
    if args.check_scope == "closure":
        closure = driver_closure(
            entrypoints,
            getattr(parsed_context, "call_graph", {}),
            set(discovery.driver_functions),
        )
        functions_to_check = list(dict.fromkeys([*entrypoints, *sorted(closure - set(entrypoints))]))
    else:
        functions_to_check = entrypoints
    if harness_names:
        functions_to_check = list(dict.fromkeys([*harness_names, *functions_to_check]))
    if args.max_functions is not None:
        functions_to_check = functions_to_check[: max(0, args.max_functions)]
    if not functions_to_check:
        raise SystemExit("no functions selected for checking")

    run_source_i = materialize_kernel_mode_source(
        pkg,
        discovery,
        output,
        harnesses=generated_harnesses,
        devm_regulator_callbacks=devm_regulator_callbacks,
    )
    manifest["run_source_i"] = str(run_source_i)
    manifest["selected_entrypoints"] = [entry.__dict__ for entry in selected_entries]
    manifest["check_scope"] = args.check_scope
    manifest["functions_to_check"] = functions_to_check
    manifest["generated_harnesses"] = [h.to_dict() for h in generated_harnesses]
    driver_name = args.driver or pkg.package.parent.name + "_" + pkg.package.name
    available_generated_shims = set(GENERATED_SHIM_FUNCTIONS) & set(
        getattr(parsed_context, "functions", {}).keys()
    )
    manifest["environment_shims_in_scope"] = sorted(available_generated_shims)
    manifest["devm_action_callbacks_in_scope"] = sorted(devm_action_callbacks)
    manifest["devm_regulator_callbacks_in_scope"] = sorted(devm_regulator_callbacks)
    (output / "kernel_mode_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    driver_scope = (
        set(discovery.driver_functions)
        | set(harness_names)
        | available_generated_shims
        | devm_action_callbacks
    )

    start = time.time()
    force_inline = (
        {name for harness in generated_harnesses for name in harness.entrypoints}
        | available_generated_shims
        | devm_action_callbacks
    )
    no_llm = bool(args.minimal or getattr(config, "disable_llm_fallbacks", False))
    with (
        kernel_scope_patch(run_source_i, driver_scope, driver_entrypoints=selected_entries),
        lifecycle_inline_patch(force_inline),
        no_external_llm_patch(no_llm),
    ):
        pipeline = AMCPipeline(config)
        reports = pipeline.run(
            source_file=str(run_source_i),
            driver_name=driver_name,
            only_functions=set(functions_to_check),
            analysis_source_file=str(pkg.analysis_source),
        )

    summary = {
        "driver_name": driver_name,
        "entrypoints_checked": entrypoints,
        "functions_checked": functions_to_check,
        "check_scope": args.check_scope,
        "generated_harnesses": harness_names,
        "reports": len(reports),
        "duration_sec": round(time.time() - start, 3),
        "artifact_dir": str(output),
    }
    (output / "kernel_mode_run_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_error": str(exc)}
    return data if isinstance(data, dict) else {"_value": data}


def _inner_cbmc_result(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("result")
    return inner if isinstance(inner, dict) else payload


def _property_type(failing_property: str) -> str:
    parts = str(failing_property or "").split(".")
    return parts[-2] if len(parts) >= 2 else str(failing_property or "unknown")


def _parse_csv(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def collect_cbmc_results(run_output: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(run_output.glob("*/*/cbmc_result.json")):
        payload = _read_json(path)
        result = _inner_cbmc_result(payload)
        cexs = result.get("counterexamples") or []
        if not isinstance(cexs, list):
            cexs = []
        prop_counts: dict[str, int] = {}
        properties: list[str] = []
        no_body_count = 0
        for cex in cexs:
            if not isinstance(cex, dict):
                continue
            prop = str(cex.get("failing_property") or "unknown")
            properties.append(prop)
            kind = _property_type(prop)
            prop_counts[kind] = prop_counts.get(kind, 0) + 1
            if kind == "no-body":
                no_body_count += 1
        rows.append(
            {
                "driver": path.parent.parent.name,
                "function": path.parent.name,
                "verified": bool(result.get("verified")),
                "error": result.get("error"),
                "counterexamples": len(cexs),
                "no_body_counterexamples": no_body_count,
                "non_no_body_counterexamples": max(0, len(cexs) - no_body_count),
                "property_types": prop_counts,
                "sample_properties": properties[:8],
                "artifact": str(path),
            }
        )
    return rows


def collect_run_summary(run_output: Path) -> dict[str, Any]:
    manifest = _read_json(run_output / "kernel_mode_manifest.json")
    run_summary = _read_json(run_output / "kernel_mode_run_summary.json")
    cbmc_rows = collect_cbmc_results(run_output)
    total_cex = sum(int(row["counterexamples"]) for row in cbmc_rows)
    total_non_no_body = sum(int(row["non_no_body_counterexamples"]) for row in cbmc_rows)
    total_no_body = sum(int(row["no_body_counterexamples"]) for row in cbmc_rows)
    cbmc_errors = [row for row in cbmc_rows if row.get("error")]
    cbmc_failed = [row for row in cbmc_rows if not row.get("verified") and not row.get("error")]
    return {
        "output": str(run_output),
        "driver_name": run_summary.get("driver_name"),
        "duration_sec": run_summary.get("duration_sec"),
        "reports": run_summary.get("reports", 0),
        "entrypoints_checked": run_summary.get("entrypoints_checked", []),
        "functions_checked": run_summary.get("functions_checked", run_summary.get("entrypoints_checked", [])),
        "check_scope": run_summary.get("check_scope", "entrypoints"),
        "generated_harnesses": run_summary.get("generated_harnesses", []),
        "discovered_entrypoints": [
            entry.get("name")
            for entry in (manifest.get("entrypoints") or [])
            if isinstance(entry, dict)
        ],
        "driver_functions": len(manifest.get("driver_functions") or []),
        "driver_closure": len(manifest.get("driver_closure") or []),
        "external_symbols": len(manifest.get("external_symbols") or []),
        "ignored_wrappers": manifest.get("ignored_wrappers") or [],
        "warnings": manifest.get("warnings") or [],
        "cbmc": {
            "functions": cbmc_rows,
            "checked": len(cbmc_rows),
            "verified": sum(1 for row in cbmc_rows if row.get("verified")),
            "failed": len(cbmc_failed),
            "errors": len(cbmc_errors),
            "counterexamples": total_cex,
            "no_body_counterexamples": total_no_body,
            "non_no_body_counterexamples": total_non_no_body,
        },
    }


def iter_packages(root: Path, variants: set[str], cases: set[str] | None = None) -> list[Path]:
    packages: list[Path] = []
    for case_dir in sorted(root.glob("*")):
        if not case_dir.is_dir():
            continue
        if cases and case_dir.name not in cases:
            continue
        for variant in sorted(variants):
            package = case_dir / variant
            if (
                package.is_dir()
                and (package / "provenance.json").exists()
                and (package / "analysis" / "analysis_context.c").exists()
                and any((package / "slice").glob("*.i"))
            ):
                packages.append(package)
    return packages


def _default_cbmc_env() -> dict[str, str]:
    env = dict(os.environ)
    if "BMC_AGENT_CBMC_PATH" not in env:
        local_cbmc = REPO_ROOT.parent / "tools" / "cbmc" / "usr" / "bin" / "cbmc"
        if local_cbmc.exists():
            env["BMC_AGENT_CBMC_PATH"] = str(local_cbmc)
    return env


def _text_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_command_group(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> tuple[int, str, str, bool]:
    """Run a command in its own process group and kill the group on timeout."""
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        return (
            124,
            _text_or_empty(exc.stdout) + _text_or_empty(stdout),
            _text_or_empty(exc.stderr) + _text_or_empty(stderr),
            True,
        )


def cmd_batch(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    variants = {v.strip() for v in args.variants.split(",") if v.strip()}
    cases = {c.strip() for c in args.cases.split(",") if c.strip()} if args.cases else None
    packages = iter_packages(root, variants, cases=cases)
    if args.limit is not None:
        packages = packages[: max(0, args.limit)]
    if not packages:
        raise SystemExit(f"no packages found under {root}")

    batch_output = Path(args.output).resolve()
    if args.require_clean_audit:
        audit = audit_packages(root, variants, cases=cases)
        audit_dir = batch_output / "preflight_package_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "package_audit.json").write_text(
            json.dumps(audit, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (audit_dir / "package_audit.md").write_text(
            render_audit_markdown(audit),
            encoding="utf-8",
        )
        if audit["issue_count"]:
            raise SystemExit(
                "package audit failed before batch run; see "
                f"{audit_dir / 'package_audit.md'}"
            )

    logs_dir = batch_output / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    start = time.time()
    for idx, package in enumerate(packages, 1):
        case_name = package.parent.name
        variant = package.name
        case_output = batch_output / case_name / variant
        log_path = logs_dir / f"{case_name}__{variant}.log"
        discovery = discover(
            package,
            include_suffix_fallback=not args.no_suffix_fallback,
            max_public=args.max_public,
        )
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--package",
            str(package),
            "--output",
            str(case_output),
            "--max-public",
            str(args.max_public),
            "--threat-model",
            args.threat_model,
        ]
        if args.minimal:
            cmd.append("--minimal")
        if args.lite_mode:
            cmd.append("--lite-mode")
        if args.no_realism_check:
            cmd.append("--no-realism-check")
        if args.phase3_property_allowlist:
            cmd.extend(["--phase3-property-allowlist", args.phase3_property_allowlist])
        if args.no_suffix_fallback:
            cmd.append("--no-suffix-fallback")
        if args.lifecycle_harness:
            cmd.append("--lifecycle-harness")
        if args.max_entrypoints is not None:
            cmd.extend(["--max-entrypoints", str(args.max_entrypoints)])
        cmd.extend(["--entrypoint-policy", args.entrypoint_policy])
        cmd.extend(["--check-scope", args.check_scope])
        if args.max_functions is not None:
            cmd.extend(["--max-functions", str(args.max_functions)])

        print(f"[{idx}/{len(packages)}] {case_name}/{variant}", file=sys.stderr)
        returncode, stdout, stderr, timed_out = run_command_group(
            cmd,
            cwd=REPO_ROOT,
            env=_default_cbmc_env(),
            timeout=args.timeout_per_case,
        )
        log_path.write_text(
            "COMMAND: " + " ".join(cmd) + "\n\n"
            + (f"TIMED OUT after {args.timeout_per_case}s\n\n" if timed_out else "")
            + "STDOUT:\n" + stdout + "\n\n"
            + "STDERR:\n" + stderr,
            encoding="utf-8",
        )
        error = (
            f"timeout after {args.timeout_per_case}s"
            if timed_out
            else (None if returncode == 0 else f"exit {returncode}")
        )

        run_summary = collect_run_summary(case_output) if case_output.exists() else {}
        row = {
            "case": case_name,
            "variant": variant,
            "package": str(package),
            "output": str(case_output),
            "log": str(log_path),
            "returncode": returncode,
            "timed_out": timed_out,
            "error": error,
            "discovery": {
                "driver_functions": len(discovery.driver_functions),
                "entrypoints": [entry.__dict__ for entry in discovery.entrypoints],
                "driver_closure": len(discovery.driver_closure),
                "external_symbols": len(discovery.external_symbols),
                "ignored_wrappers": discovery.ignored_wrappers,
                "warnings": discovery.warnings,
            },
            "run": run_summary,
        }
        rows.append(row)
        (batch_output / "batch_summary.json").write_text(
            json.dumps(batch_aggregate(rows, start), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    summary = batch_aggregate(rows, start)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if all(row["returncode"] == 0 for row in rows) else 1


def batch_aggregate(rows: list[dict[str, Any]], start: float) -> dict[str, Any]:
    cbmc_checked = 0
    cbmc_verified = 0
    cbmc_failed = 0
    cbmc_errors = 0
    no_body = 0
    non_no_body = 0
    reports = 0
    for row in rows:
        run = row.get("run") or {}
        cbmc = (run.get("cbmc") or {}) if isinstance(run, dict) else {}
        cbmc_checked += int(cbmc.get("checked") or 0)
        cbmc_verified += int(cbmc.get("verified") or 0)
        cbmc_failed += int(cbmc.get("failed") or 0)
        cbmc_errors += int(cbmc.get("errors") or 0)
        no_body += int(cbmc.get("no_body_counterexamples") or 0)
        non_no_body += int(cbmc.get("non_no_body_counterexamples") or 0)
        reports += int(run.get("reports") or 0) if isinstance(run, dict) else 0
    return {
        "duration_sec": round(time.time() - start, 3),
        "total_cases": len(rows),
        "succeeded": sum(1 for row in rows if row.get("returncode") == 0),
        "failed": sum(1 for row in rows if row.get("returncode") not in (0, 124)),
        "timed_out": sum(1 for row in rows if row.get("timed_out")),
        "reports": reports,
        "cbmc": {
            "checked": cbmc_checked,
            "verified": cbmc_verified,
            "failed": cbmc_failed,
            "errors": cbmc_errors,
            "no_body_counterexamples": no_body,
            "non_no_body_counterexamples": non_no_body,
        },
        "rows": rows,
    }


def normalize_description(description: str) -> str:
    desc = re.sub(r"\s+", " ", str(description or "")).strip()
    # Line/property indices are unstable across buggy/fixed variants. Keep the
    # source-level failure shape, not CBMC numbering.
    desc = re.sub(r"\b\d+\b", "<n>", desc)
    return desc


def failure_source_line(cex: dict[str, Any]) -> str:
    """Return the normalized source text at a CBMC failure location if local.

    Paired fixed-bug triage compares buggy and fixed counterexamples by
    signature. Description-only signatures can be too coarse for pointer
    failures around a fix: a buggy pre-guard dereference and a fixed post-guard
    partial-ops-table dereference may have the same CBMC description but occur
    on different source statements. Including the local failure line keeps the
    comparison source-level without relying on unstable CBMC property numbers.
    """
    loc = cex.get("failure_location") or {}
    if not isinstance(loc, dict):
        return ""
    path_text = str(loc.get("file") or "")
    line_text = str(loc.get("line") or "")
    if not path_text or not line_text.isdigit():
        return ""
    path = Path(path_text)
    if not path.exists() or not path.is_file():
        return ""
    line_no = int(line_text)
    if line_no <= 0:
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for idx, raw in enumerate(f, 1):
                if idx == line_no:
                    return normalize_description(raw)
    except OSError:
        return ""
    return ""


def cex_signature(function: str, cex: dict[str, Any]) -> str:
    prop = str(cex.get("failing_property") or "")
    prop_type = _property_type(prop)
    desc = normalize_description(str(cex.get("description") or ""))
    loc = cex.get("failure_location") or {}
    loc_fn = loc.get("function") if isinstance(loc, dict) else ""
    src = failure_source_line(cex)
    return f"{function}|{prop_type}|{loc_fn}|{desc}|{src}"


def cex_root_signature(function: str, cex: dict[str, Any]) -> str:
    """Coarser signature for deduplicating one root cause's CBMC sub-checks."""
    prop = str(cex.get("failing_property") or "")
    prop_type = _property_type(prop)
    desc = normalize_description(str(cex.get("description") or ""))
    loc = cex.get("failure_location") or {}
    loc_fn = loc.get("function") if isinstance(loc, dict) else ""
    src = failure_source_line(cex)
    root = src or desc
    return f"{function}|{prop_type}|{loc_fn}|{root}"


def collect_counterexamples_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    run = row.get("run") or {}
    cbmc = run.get("cbmc") or {}
    records: list[dict[str, Any]] = []
    for fn_row in cbmc.get("functions") or []:
        artifact = Path(str(fn_row.get("artifact") or ""))
        if not artifact.exists():
            continue
        payload = _read_json(artifact)
        result = _inner_cbmc_result(payload)
        function = str(fn_row.get("function") or artifact.parent.name)
        for idx, cex in enumerate(result.get("counterexamples") or []):
            if not isinstance(cex, dict):
                continue
            prop = str(cex.get("failing_property") or "unknown")
            prop_type = _property_type(prop)
            records.append(
                {
                    "case": row.get("case"),
                    "variant": row.get("variant"),
                    "function": function,
                    "index": idx,
                    "failing_property": prop,
                    "property_type": prop_type,
                    "description": str(cex.get("description") or ""),
                    "failure_location": cex.get("failure_location") or {},
                    "signature": cex_signature(function, cex),
                    "root_signature": cex_root_signature(function, cex),
                    "failure_source_line": failure_source_line(cex),
                    "artifact": str(artifact),
                    "is_no_body": prop_type == "no-body",
                }
            )
    return records


SAFETY_PROPERTY_TYPES = {
    "pointer_dereference",
    "bounds",
    "overflow",
    "undefined-shift",
    "pointer_primitives",
}


def score_candidate(
    record: dict[str, Any],
    *,
    present_in_fixed: bool,
    present_in_buggy: bool,
    fixed_control_available: bool,
    selection_mode: str,
) -> tuple[int, str]:
    if record.get("is_no_body"):
        return 0, "skip:no-body kernel/harness artifact"
    if selection_mode == "paired" and record.get("variant") != "buggy":
        return 0, "skip:fixed-variant"
    prop_type = record.get("property_type")
    score = 35 if selection_mode == "unknown" else 40
    reasons = [f"mode:{selection_mode}"]
    if prop_type in SAFETY_PROPERTY_TYPES:
        score += 20
        reasons.append(f"safety-property:{prop_type}")
    if prop_type == "assertion":
        score += 15
        reasons.append("assertion-property")
    if str(record.get("failing_property") or "").startswith("bmc_agent_"):
        score += 10
        reasons.append("framework-contract")
    function_name = str(record.get("function") or "")
    if function_name.startswith(("bmc_agent_lifecycle_", "bmc_agent_error_cleanup_")):
        score += 10
        reasons.append("framework-harness-root")
    if selection_mode == "paired" and record.get("variant") == "buggy" and fixed_control_available and not present_in_fixed:
        score += 35
        reasons.append("buggy-only-vs-fixed")
    elif selection_mode == "paired" and record.get("variant") == "buggy" and fixed_control_available and present_in_fixed:
        score -= 25
        reasons.append("also-in-fixed")
    elif selection_mode == "paired" and record.get("variant") == "buggy" and not fixed_control_available:
        reasons.append("no-fixed-control")
    if "nondet" in normalize_description(record.get("description", "")).lower():
        score -= 10
        reasons.append("nondet-shaped")
    return max(score, 0), ",".join(reasons) or "generic"


def candidate_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    desc = normalize_description(record.get("description", "")).lower()
    return (
        -int(record.get("score", 0)),
        0 if "pointer null" in desc else 1,
        0 if "assertion-property" in str(record.get("reason") or "") else 1,
        str(record.get("case")),
        str(record.get("variant")),
        str(record.get("function")),
        str(record.get("failing_property")),
    )


def dedupe_candidate_roots(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one representative candidate per source-level root."""
    best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for record in candidates:
        key = (
            str(record.get("case")),
            str(record.get("variant")),
            str(record.get("function")),
            str(record.get("root_signature") or record.get("signature")),
        )
        current = best.get(key)
        if current is None or candidate_sort_key(record) < candidate_sort_key(current):
            best[key] = record
    return list(best.values())


def resolve_candidate_selection_mode(
    requested: str,
    records_by_case_variant: dict[tuple[str, str], list[dict[str, Any]]],
) -> str:
    if requested in {"paired", "unknown"}:
        return requested
    variants = {variant for _case, variant in records_by_case_variant}
    return "paired" if "fixed" in variants else "unknown"


def triage_batch(
    summary_path: Path,
    *,
    max_candidates: int,
    min_score: int,
    candidate_selection_mode: str = "auto",
) -> dict[str, Any]:
    summary = _read_json(summary_path)
    rows = summary.get("rows") or []
    records_by_case_variant: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("case")), str(row.get("variant")))
        records_by_case_variant.setdefault(key, []).extend(collect_counterexamples_from_row(row))

    selection_mode = resolve_candidate_selection_mode(candidate_selection_mode, records_by_case_variant)
    case_names = sorted({case for case, _variant in records_by_case_variant})
    case_summaries: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for case in case_names:
        buggy = records_by_case_variant.get((case, "buggy"), [])
        fixed = records_by_case_variant.get((case, "fixed"), [])
        if selection_mode == "paired":
            records_to_score = buggy + fixed
        else:
            records_to_score = [
                record
                for (record_case, _variant), records in records_by_case_variant.items()
                if record_case == case
                for record in records
            ]
        buggy_sigs = {r["signature"] for r in buggy}
        fixed_sigs = {r["signature"] for r in fixed}
        shared = buggy_sigs & fixed_sigs
        buggy_only = buggy_sigs - fixed_sigs
        fixed_only = fixed_sigs - buggy_sigs
        fixed_control_available = bool(fixed)
        case_summaries.append(
            {
                "case": case,
                "buggy_counterexamples": len(buggy),
                "fixed_counterexamples": len(fixed),
                "buggy_non_no_body": sum(1 for r in buggy if not r["is_no_body"]),
                "fixed_non_no_body": sum(1 for r in fixed if not r["is_no_body"]),
                "shared_signatures": len(shared),
                "buggy_only_signatures": len(buggy_only),
                "fixed_only_signatures": len(fixed_only),
                "fixed_control_available": fixed_control_available,
            }
        )
        for record in records_to_score:
            score, reason = score_candidate(
                record,
                present_in_fixed=record["signature"] in fixed_sigs,
                present_in_buggy=record["signature"] in buggy_sigs,
                fixed_control_available=fixed_control_available,
                selection_mode=selection_mode,
            )
            if score < min_score:
                continue
            enriched = dict(record)
            enriched["score"] = score
            enriched["reason"] = reason
            enriched["present_in_fixed"] = record["signature"] in fixed_sigs
            enriched["present_in_buggy"] = record["signature"] in buggy_sigs
            candidates.append(enriched)

    raw_candidate_count = len(candidates)
    candidates = dedupe_candidate_roots(candidates)
    candidates.sort(key=candidate_sort_key)
    selected = candidates[:max_candidates]
    return {
        "source_summary": str(summary_path),
        "batch": {
            "total_cases": summary.get("total_cases"),
            "succeeded": summary.get("succeeded"),
            "timed_out": summary.get("timed_out"),
            "cbmc": summary.get("cbmc"),
        },
        "case_summaries": case_summaries,
        "min_score": min_score,
        "candidate_selection_mode": selection_mode,
        "candidate_selection_mode_requested": candidate_selection_mode,
        "raw_candidate_count": raw_candidate_count,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected_candidates": selected,
    }


def render_triage_markdown(triage: dict[str, Any]) -> str:
    lines = [
        "# Kernel-Aware Driver CBMC Candidate Triage",
        "",
        f"Source summary: `{triage['source_summary']}`",
        f"Candidate selection mode: `{triage.get('candidate_selection_mode')}`",
        f"Requested mode: `{triage.get('candidate_selection_mode_requested')}`",
        "",
        "## Case Summary",
        "",
        "| case | fixed control | buggy cex | fixed cex | buggy non-no-body | fixed non-no-body | shared sigs | buggy-only sigs | fixed-only sigs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in triage.get("case_summaries") or []:
        lines.append(
            f"| {row['case']} | {row.get('fixed_control_available')} "
            f"| {row['buggy_counterexamples']} | {row['fixed_counterexamples']} "
            f"| {row['buggy_non_no_body']} | {row['fixed_non_no_body']} "
            f"| {row['shared_signatures']} | {row['buggy_only_signatures']} | {row['fixed_only_signatures']} |"
        )
    lines.extend([
        "",
        "## Selected Candidates",
        "",
        "| score | case | variant | function | property | reason | also in fixed | description |",
        "|---:|---|---|---|---|---|---:|---|",
    ])
    for row in triage.get("selected_candidates") or []:
        desc = str(row.get("description") or "").replace("|", "\\|")
        if len(desc) > 140:
            desc = desc[:137].rstrip() + "..."
        lines.append(
            f"| {row['score']} | {row['case']} | {row['variant']} | {row['function']} "
            f"| {row['failing_property']} | {row['reason']} | {row['present_in_fixed']} | {desc} |"
        )
    if not triage.get("selected_candidates"):
        lines.append("| | | | | | no candidates selected | | |")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "`paired` mode is for benchmark adjudication with buggy/fixed controls. "
        "High-priority paired candidates should be non-no-body findings that appear in the buggy variant but not the fixed variant. "
        "`unknown` mode is for realistic bug finding: candidates come from the cheap scan of the analyzed source itself, "
        "with no fixed-side oracle. In unknown mode, fixed-control columns are descriptive only and must not be read as discovery evidence.",
        "",
    ])
    return "\n".join(lines)


def cmd_triage(args: argparse.Namespace) -> int:
    summary_path = Path(args.batch_summary).resolve()
    if not summary_path.exists():
        raise SystemExit(f"batch summary not found: {summary_path}")
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    triage = triage_batch(
        summary_path,
        max_candidates=args.max_candidates,
        min_score=args.min_score,
        candidate_selection_mode=args.candidate_selection_mode,
    )
    json_path = out_dir / "candidate_triage.json"
    md_path = out_dir / "candidate_triage.md"
    json_path.write_text(json.dumps(triage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_triage_markdown(triage) + "\n", encoding="utf-8")
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "candidate_count": triage["candidate_count"],
        "selected_count": triage["selected_count"],
    }, indent=2, sort_keys=True))
    return 0


def _first_i_file(package: Path) -> Path | None:
    files = sorted((package / "slice").glob("*.i"))
    return files[0] if files else None


def package_audit_row(package: Path) -> dict[str, Any]:
    row: dict[str, Any] = {
        "package": str(package),
        "exists": package.exists(),
        "issues": [],
        "warnings": [],
    }
    if not package.exists():
        row["issues"].append("package directory missing")
        return row

    provenance_path = package / "provenance.json"
    analysis_path = package / "analysis" / "analysis_context.c"
    source_i = _first_i_file(package)
    row["provenance_path"] = str(provenance_path)
    row["analysis_path"] = str(analysis_path)
    row["source_i"] = str(source_i) if source_i else None

    if not provenance_path.exists():
        row["issues"].append("missing provenance.json")
        provenance: dict[str, Any] = {}
    else:
        provenance = _read_json(provenance_path)
    if not analysis_path.exists():
        row["issues"].append("missing analysis/analysis_context.c")
    if source_i is None:
        row["issues"].append("missing slice/*.i")

    row["status"] = provenance.get("status")
    if provenance.get("status") not in {None, "ready"}:
        row["issues"].append(f"provenance status is {provenance.get('status')!r}")
    case = provenance.get("case") or {}
    row["case_id"] = case.get("case_id") or package.parent.name
    row["variant"] = provenance.get("variant") or package.name
    row["source_rel"] = case.get("source_rel")
    row["target_function"] = case.get("target_function")
    row["analysis_sha256"] = (provenance.get("analysis_context") or {}).get("sha256")
    row["source_i_sha256"] = (provenance.get("preprocessed_i") or {}).get("sha256")
    copied = provenance.get("copied_sources") or []
    row["primary_source_sha256"] = copied[0].get("sha256") if copied else None
    row["source_overlay"] = provenance.get("source_overlay")
    overlay = row["source_overlay"]
    if isinstance(overlay, dict) and overlay.get("before_sha256") == overlay.get("after_sha256"):
        row["issues"].append("source_overlay did not change the source file")

    try:
        discovery = discover(package)
        row["driver_functions"] = len(discovery.driver_functions)
        row["entrypoints"] = [entry.__dict__ for entry in discovery.entrypoints]
        row["entrypoint_count"] = len(discovery.entrypoints)
        row["driver_closure"] = len(discovery.driver_closure)
        row["external_symbols"] = len(discovery.external_symbols)
        row["ignored_wrappers"] = discovery.ignored_wrappers
        row["warnings"].extend(discovery.warnings)
        parsed, _ = parse_driver_context(Path(discovery.analysis_source))
        row["generated_lifecycle_harnesses"] = [
            harness.to_dict()
            for harness in generated_lifecycle_harnesses(discovery, parsed)
        ]
    except Exception as exc:
        row["issues"].append(f"discovery failed: {exc}")
    return row


def audit_packages(root: Path, variants: set[str], cases: set[str] | None = None) -> dict[str, Any]:
    packages = iter_packages(root, variants, cases=cases)
    rows = [package_audit_row(package) for package in packages]
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_case.setdefault(str(row.get("case_id")), {})[str(row.get("variant"))] = row

    pair_issues: list[dict[str, Any]] = []
    for case_id, case_rows in sorted(by_case.items()):
        buggy = case_rows.get("buggy")
        fixed = case_rows.get("fixed")
        if not buggy or not fixed:
            continue
        comparisons = [
            ("analysis_sha256", "buggy/fixed analysis_context hashes are identical"),
            ("source_i_sha256", "buggy/fixed preprocessed .i hashes are identical"),
            ("primary_source_sha256", "buggy/fixed primary source hashes are identical"),
        ]
        for key, message in comparisons:
            if buggy.get(key) and buggy.get(key) == fixed.get(key):
                pair_issues.append({
                    "case_id": case_id,
                    "field": key,
                    "message": message,
                    "value": buggy.get(key),
                })
    issue_count = sum(len(row.get("issues") or []) for row in rows) + len(pair_issues)
    return {
        "root": str(root),
        "variants": sorted(variants),
        "cases": sorted(cases) if cases else None,
        "package_count": len(rows),
        "issue_count": issue_count,
        "packages": rows,
        "pair_issues": pair_issues,
    }


def render_audit_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Kernel-Aware Driver Package Audit",
        "",
        f"Root: `{audit['root']}`",
        f"Variants: `{','.join(audit['variants'])}`",
        f"Packages: {audit['package_count']}",
        f"Issues: {audit['issue_count']}",
        "",
        "## Packages",
        "",
        "| case | variant | status | entrypoints | lifecycle harnesses | issues | warnings |",
        "|---|---|---|---:|---:|---|---|",
    ]
    for row in audit.get("packages") or []:
        issue = "<br>".join(row.get("issues") or [])
        warning = "<br>".join(row.get("warnings") or [])
        lines.append(
            f"| {row.get('case_id')} | {row.get('variant')} | {row.get('status')} "
            f"| {row.get('entrypoint_count', 0)} | {len(row.get('generated_lifecycle_harnesses') or [])} "
            f"| {issue or ''} | {warning or ''} |"
        )
    lines.extend([
        "",
        "## Buggy/Fixed Pair Issues",
        "",
        "| case | field | issue |",
        "|---|---|---|",
    ])
    for issue in audit.get("pair_issues") or []:
        lines.append(f"| {issue['case_id']} | {issue['field']} | {issue['message']} |")
    if not audit.get("pair_issues"):
        lines.append("| | | no pair issues |")
    lines.append("")
    return "\n".join(lines)


def cmd_audit_packages(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    variants = {v.strip() for v in args.variants.split(",") if v.strip()}
    cases = {c.strip() for c in args.cases.split(",") if c.strip()} if args.cases else None
    audit = audit_packages(root, variants, cases=cases)
    out_dir = Path(args.output).resolve() if args.output else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "package_audit.json"
        md_path = out_dir / "package_audit.md"
        json_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        md_path.write_text(render_audit_markdown(audit), encoding="utf-8")
        print(json.dumps({
            "json": str(json_path),
            "markdown": str(md_path),
            "issue_count": audit["issue_count"],
            "package_count": audit["package_count"],
        }, indent=2, sort_keys=True))
    else:
        print(json.dumps(audit, indent=2, sort_keys=True))
    return 1 if args.strict and audit["issue_count"] else 0


API_MARKER_RE = re.compile(
    r"LLM usage|OpenRouter|Anthropic|Claude|OpenAI|\bopenai\b|\banthropic\b",
    re.IGNORECASE,
)


def scan_api_markers(logs_dir: Path | None) -> dict[str, Any]:
    if logs_dir is None or not logs_dir.exists():
        return {"logs_dir": str(logs_dir) if logs_dir else None, "marker_count": 0, "matches": []}
    matches: list[dict[str, Any]] = []
    for path in sorted(logs_dir.glob("*.log")):
        try:
            for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if API_MARKER_RE.search(line):
                    matches.append({
                        "path": str(path),
                        "line": lineno,
                        "text": line[:240],
                    })
        except Exception as exc:
            matches.append({"path": str(path), "line": None, "text": f"failed to read log: {exc}"})
    return {
        "logs_dir": str(logs_dir),
        "marker_count": len(matches),
        "matches": matches[:50],
        "truncated": len(matches) > 50,
    }


def case_variant_rows(batch: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in batch.get("rows") or []:
        run = row.get("run") or {}
        cbmc = run.get("cbmc") or {}
        rows.append({
            "case": row.get("case"),
            "variant": row.get("variant"),
            "returncode": row.get("returncode"),
            "timed_out": row.get("timed_out"),
            "functions_checked": run.get("functions_checked") or [],
            "generated_harnesses": run.get("generated_harnesses") or [],
            "reports": run.get("reports", 0),
            "cbmc_checked": cbmc.get("checked", 0),
            "cbmc_errors": cbmc.get("errors", 0),
            "cbmc_verified": cbmc.get("verified", 0),
            "non_no_body_counterexamples": cbmc.get("non_no_body_counterexamples", 0),
            "no_body_counterexamples": cbmc.get("no_body_counterexamples", 0),
        })
    return rows


def build_report(
    *,
    batch_summary_path: Path,
    triage_path: Path | None,
    audit_path: Path | None,
    logs_dir: Path | None,
) -> dict[str, Any]:
    batch = _read_json(batch_summary_path)
    triage = _read_json(triage_path) if triage_path and triage_path.exists() else {}
    audit = _read_json(audit_path) if audit_path and audit_path.exists() else {}
    api_scan = scan_api_markers(logs_dir)
    cbmc = batch.get("cbmc") or {}
    selected = triage.get("selected_candidates") or []
    status = "ready"
    reasons: list[str] = []
    if audit and int(audit.get("issue_count") or 0) > 0:
        status = "input_issues"
        reasons.append(f"package audit has {audit.get('issue_count')} issue(s)")
    if int(batch.get("timed_out") or 0) > 0:
        status = "run_issues"
        reasons.append(f"{batch.get('timed_out')} package run(s) timed out")
    if int(cbmc.get("errors") or 0) > 0:
        status = "run_issues"
        reasons.append(f"{cbmc.get('errors')} CBMC conversion/runtime error(s)")
    if int(api_scan.get("marker_count") or 0) > 0:
        status = "api_used_or_logged"
        reasons.append(f"{api_scan.get('marker_count')} API/LLM marker(s) found in logs")
    if not reasons:
        reasons.append("package/run artifacts are internally clean under available checks")

    return {
        "status": status,
        "status_reasons": reasons,
        "batch_summary": str(batch_summary_path),
        "triage": str(triage_path) if triage_path else None,
        "audit": str(audit_path) if audit_path else None,
        "batch": {
            "total_cases": batch.get("total_cases"),
            "succeeded": batch.get("succeeded"),
            "failed": batch.get("failed"),
            "timed_out": batch.get("timed_out"),
            "duration_sec": batch.get("duration_sec"),
            "reports": batch.get("reports"),
            "cbmc": cbmc,
        },
        "audit_summary": {
            "package_count": audit.get("package_count"),
            "issue_count": audit.get("issue_count"),
            "pair_issues": audit.get("pair_issues") or [],
        } if audit else None,
        "triage_summary": {
            "candidate_count": triage.get("candidate_count"),
            "selected_count": triage.get("selected_count"),
            "case_summaries": triage.get("case_summaries") or [],
            "selected_candidates": selected,
        } if triage else None,
        "api_marker_scan": api_scan,
        "case_variant_rows": case_variant_rows(batch),
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    batch = report.get("batch") or {}
    cbmc = batch.get("cbmc") or {}
    audit = report.get("audit_summary") or {}
    triage = report.get("triage_summary") or {}
    api = report.get("api_marker_scan") or {}
    lines = [
        "# Kernel-Aware Driver Mode Run Report",
        "",
        f"Status: `{report.get('status')}`",
        "",
        "## Status Reasons",
        "",
    ]
    for reason in report.get("status_reasons") or []:
        lines.append(f"- {reason}")
    lines.extend([
        "",
        "## Inputs",
        "",
        f"- batch summary: `{report.get('batch_summary')}`",
        f"- package audit: `{report.get('audit')}`",
        f"- triage: `{report.get('triage')}`",
        "",
        "## Run Summary",
        "",
        f"- variants run: {batch.get('total_cases')}",
        f"- succeeded: {batch.get('succeeded')}",
        f"- timed out: {batch.get('timed_out')}",
        f"- duration_sec: {batch.get('duration_sec')}",
        f"- CBMC checked roots: {cbmc.get('checked')}",
        f"- CBMC errors: {cbmc.get('errors')}",
        f"- API/LLM log markers: {api.get('marker_count')}",
        "",
        "## Package Audit",
        "",
    ])
    if audit:
        lines.extend([
            f"- packages: {audit.get('package_count')}",
            f"- issues: {audit.get('issue_count')}",
        ])
        pair_issues = audit.get("pair_issues") or []
        if pair_issues:
            lines.extend(["", "| case | field | issue |", "|---|---|---|"])
            for issue in pair_issues:
                lines.append(f"| {issue.get('case_id')} | {issue.get('field')} | {issue.get('message')} |")
    else:
        lines.append("- no audit artifact supplied")

    lines.extend([
        "",
        "## Candidate Triage",
        "",
        f"- candidates: {triage.get('candidate_count')}",
        f"- selected: {triage.get('selected_count')}",
        "",
        "| score | case | variant | function | property | description |",
        "|---:|---|---|---|---|---|",
    ])
    selected = triage.get("selected_candidates") or []
    if selected:
        for row in selected:
            desc = str(row.get("description") or "").replace("|", "\\|")
            if len(desc) > 160:
                desc = desc[:157].rstrip() + "..."
            lines.append(
                f"| {row.get('score')} | {row.get('case')} | {row.get('variant')} "
                f"| {row.get('function')} | {row.get('failing_property')} | {desc} |"
            )
    else:
        lines.append("| | | | | | no selected candidates |")

    lines.extend([
        "",
        "## Coverage",
        "",
        "| case | variant | functions checked | generated harnesses | CBMC errors | non-no-body CEX |",
        "|---|---|---|---|---:|---:|",
    ])
    for row in report.get("case_variant_rows") or []:
        funcs = ", ".join(row.get("functions_checked") or [])
        harnesses = ", ".join(row.get("generated_harnesses") or [])
        lines.append(
            f"| {row.get('case')} | {row.get('variant')} | {funcs} | {harnesses} "
            f"| {row.get('cbmc_errors')} | {row.get('non_no_body_counterexamples')} |"
        )

    if int(api.get("marker_count") or 0) > 0:
        lines.extend(["", "## API/LLM Markers", "", "| file | line | text |", "|---|---:|---|"])
        for match in api.get("matches") or []:
            text = str(match.get("text") or "").replace("|", "\\|")
            lines.append(f"| {match.get('path')} | {match.get('line')} | {text} |")
    lines.append("")
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    batch_path = Path(args.batch_summary).resolve()
    if not batch_path.exists():
        raise SystemExit(f"batch summary not found: {batch_path}")
    triage_path = Path(args.triage).resolve() if args.triage else None
    audit_path = Path(args.audit).resolve() if args.audit else None
    logs_dir = Path(args.logs_dir).resolve() if args.logs_dir else batch_path.parent / "logs"
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        batch_summary_path=batch_path,
        triage_path=triage_path,
        audit_path=audit_path,
        logs_dir=logs_dir,
    )
    json_path = out_dir / "run_report.json"
    md_path = out_dir / "run_report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_report_markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "status": report["status"],
        "selected": (report.get("triage_summary") or {}).get("selected_count"),
        "api_markers": (report.get("api_marker_scan") or {}).get("marker_count"),
    }, indent=2, sort_keys=True))
    return 0


def render_pipeline_markdown(summary: dict[str, Any]) -> str:
    report = summary.get("report") or {}
    batch = report.get("batch") or {}
    cbmc = batch.get("cbmc") or {}
    triage = report.get("triage_summary") or {}
    lines = [
        "# Kernel-Aware Driver Pipeline Summary",
        "",
        f"Status: `{summary.get('status')}`",
        f"Duration: {summary.get('duration_sec')} sec",
        f"External LLM allowed: `{summary.get('allow_llm')}`",
        "",
        "## Artifacts",
        "",
        f"- batch summary: `{summary.get('batch_summary')}`",
        f"- package audit: `{summary.get('package_audit')}`",
        f"- triage: `{summary.get('triage')}`",
        f"- report: `{summary.get('run_report')}`",
        "",
        "## Run Summary",
        "",
        f"- variants run: {batch.get('total_cases')}",
        f"- succeeded: {batch.get('succeeded')}",
        f"- timed out: {batch.get('timed_out')}",
        f"- CBMC checked roots: {cbmc.get('checked')}",
        f"- CBMC errors: {cbmc.get('errors')}",
        f"- selected candidates: {triage.get('selected_count')}",
        "",
        "## Status Reasons",
        "",
    ]
    for reason in report.get("status_reasons") or []:
        lines.append(f"- {reason}")
    lines.append("")
    return "\n".join(lines)


def selected_candidate_groups(triage: dict[str, Any]) -> list[dict[str, Any]]:
    """Group selected triage rows by package rerun unit.

    A targeted realism rerun should pay Phase-3 cost once per case/variant and
    validate only the selected failing_property values from the cheap scan.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for cand in triage.get("selected_candidates") or []:
        if not isinstance(cand, dict):
            continue
        case = str(cand.get("case") or "")
        variant = str(cand.get("variant") or "")
        prop = str(cand.get("failing_property") or "")
        if not case or not variant or not prop:
            continue
        key = (case, variant)
        group = grouped.setdefault(
            key,
            {
                "case": case,
                "variant": variant,
                "properties": [],
                "functions": [],
                "candidates": [],
            },
        )
        if prop not in group["properties"]:
            group["properties"].append(prop)
        function = str(cand.get("function") or "")
        if function and function not in group["functions"]:
            group["functions"].append(function)
        group["candidates"].append(cand)

    result = list(grouped.values())
    result.sort(key=lambda row: (str(row["case"]), str(row["variant"])))
    for row in result:
        row["properties"].sort()
        row["functions"].sort()
    return result


def render_targeted_plan_markdown(plan: dict[str, Any]) -> str:
    groups = plan.get("groups") or []
    lines = [
        "# Targeted Realism Plan",
        "",
        f"Source triage: `{plan.get('triage')}`",
        f"Candidate selection mode: `{plan.get('candidate_selection_mode')}`",
        f"Groups: {len(groups)}",
        f"Selected candidates: {plan.get('selected_count')}",
        "",
        "## Rerun Groups",
        "",
        "| case | variant | selected properties | selected functions | candidates |",
        "|---|---|---|---|---:|",
    ]
    if groups:
        for group in groups:
            props = "<br>".join(group.get("properties") or [])
            funcs = "<br>".join(group.get("functions") or [])
            lines.append(
                f"| {group.get('case')} | {group.get('variant')} | {props} "
                f"| {funcs} | {len(group.get('candidates') or [])} |"
            )
    else:
        lines.append("| | | | | 0 |")
    lines.extend([
        "",
        "## Scheduling Rule",
        "",
        "Run the first pass in minimal/no-LLM mode, rank buggy/fixed differences, "
        "then rerun full Phase 3 only for the exact failing_property values above. "
        "Do not spend realism/tool/refinement budget on fixed-variant or shared "
        "counterexamples unless a separate debugging question needs them.",
        "",
    ])
    return "\n".join(lines)


def evaluate_api_spend_gate(
    *,
    scan_report: dict[str, Any],
    triage: dict[str, Any],
    plan: dict[str, Any],
    allow_targeted_llm: bool,
) -> dict[str, Any]:
    """Summarize whether a no-API scan justifies targeted LLM spend."""
    scan_batch = scan_report.get("batch") or {}
    cbmc = scan_batch.get("cbmc") or {}
    selected = triage.get("selected_candidates") or []
    groups = plan.get("groups") or []
    mode = triage.get("candidate_selection_mode")
    blockers: list[str] = []
    warnings: list[str] = []

    if scan_report.get("status") != "ready":
        blockers.append(f"scan report status is {scan_report.get('status')!r}, not 'ready'")
    marker_count = int((scan_report.get("api_marker_scan") or {}).get("marker_count") or 0)
    if marker_count:
        blockers.append(f"cheap scan already logged {marker_count} API/LLM marker(s)")
    if int((cbmc or {}).get("errors") or 0):
        blockers.append(f"CBMC reported {cbmc.get('errors')} conversion/runtime error(s)")
    if int(scan_batch.get("timed_out") or 0):
        blockers.append(f"{scan_batch.get('timed_out')} scan variant(s) timed out")
    if not selected:
        blockers.append("no selected candidate from cheap triage")
    if selected and int((cbmc or {}).get("non_no_body_counterexamples") or 0) == 0:
        blockers.append("selected candidates exist but scan has no non-no-body counterexamples")

    if mode == "paired":
        for row in selected:
            if row.get("variant") != "buggy":
                blockers.append(f"paired candidate {row.get('case')} is not from buggy variant")
            if row.get("present_in_fixed"):
                blockers.append(f"paired candidate {row.get('case')} is also present in fixed variant")
            if "buggy-only-vs-fixed" not in str(row.get("reason") or ""):
                blockers.append(f"paired candidate {row.get('case')} lacks buggy-only-vs-fixed evidence")
    elif mode == "unknown":
        for row in selected:
            if row.get("variant") == "fixed":
                blockers.append(f"unknown-mode candidate {row.get('case')} came from a fixed variant")
    else:
        warnings.append(f"candidate selection mode is {mode!r}; prefer explicit paired or unknown")

    high_signal = []
    for row in selected:
        reason = str(row.get("reason") or "")
        prop = str(row.get("failing_property") or "")
        high_signal.append(
            any(
                token in reason or prop.startswith("bmc_agent_")
                for token in (
                    "framework-contract",
                    "lifecycle-root",
                    "safety-property:",
                    "assertion-property",
                )
            )
        )
    if selected and not any(high_signal):
        blockers.append("selected candidates are generic low-context properties")

    eligible = not blockers
    if not eligible:
        decision = "hold"
        next_action = "do not spend API; refine package, harness, or contract family first"
    elif allow_targeted_llm:
        decision = "targeted_stage_ran"
        next_action = "review targeted reports and maintainer-facing evidence before expanding"
    else:
        decision = "go_for_targeted_realism"
        next_action = "run targeted-pipeline again with --allow-targeted-llm and the same candidate cap"

    estimated_llm_calls = len(groups) * 2 if eligible else 0
    return {
        "decision": decision,
        "eligible_for_targeted_llm": eligible,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "candidate_selection_mode": mode,
        "selected_candidates": len(selected),
        "rerun_groups": len(groups),
        "estimated_llm_calls": estimated_llm_calls,
        "estimated_llm_calls_basis": (
            "conservative upper bound: up to classification + realism per rerun group"
            if eligible
            else "not eligible for targeted LLM"
        ),
        "next_action": next_action,
    }


def write_targeted_plan(triage_path: Path, out_dir: Path) -> dict[str, Any]:
    triage = _read_json(triage_path)
    groups = selected_candidate_groups(triage)
    plan = {
        "triage": str(triage_path),
        "candidate_selection_mode": triage.get("candidate_selection_mode"),
        "candidate_selection_mode_requested": triage.get("candidate_selection_mode_requested"),
        "selected_count": len(triage.get("selected_candidates") or []),
        "groups": groups,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "targeted_realism_plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "targeted_realism_plan.md").write_text(
        render_targeted_plan_markdown(plan),
        encoding="utf-8",
    )
    return plan


def run_targeted_realism_groups(args: argparse.Namespace, groups: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    root = Path(args.root).resolve()
    target_dir = Path(args.output).resolve() / "targeted_realism"
    logs_dir = target_dir / "logs"
    runs_dir = target_dir / "runs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    start = time.time()
    for idx, group in enumerate(groups, 1):
        case = str(group["case"])
        variant = str(group["variant"])
        package = root / case / variant
        if not package.exists():
            rows.append(
                {
                    "case": case,
                    "variant": variant,
                    "package": str(package),
                    "output": None,
                    "log": None,
                    "returncode": 2,
                    "timed_out": False,
                    "error": "package not found",
                    "selected_properties": group.get("properties") or [],
                    "selected_functions": group.get("functions") or [],
                    "run": {},
                }
            )
            continue

        case_output = runs_dir / case / variant
        log_path = logs_dir / f"{case}__{variant}.log"
        prop_allowlist = ",".join(group.get("properties") or [])
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--package",
            str(package),
            "--output",
            str(case_output),
            "--max-public",
            str(args.max_public),
            "--threat-model",
            args.threat_model,
            "--phase3-property-allowlist",
            prop_allowlist,
        ]
        if args.targeted_lite_mode:
            cmd.append("--lite-mode")
        if args.no_suffix_fallback:
            cmd.append("--no-suffix-fallback")
        if args.lifecycle_harness:
            cmd.append("--lifecycle-harness")
        if args.max_entrypoints is not None:
            cmd.extend(["--max-entrypoints", str(args.max_entrypoints)])
        cmd.extend(["--entrypoint-policy", args.entrypoint_policy])
        cmd.extend(["--check-scope", args.check_scope])
        if args.max_functions is not None:
            cmd.extend(["--max-functions", str(args.max_functions)])

        print(
            f"[targeted {idx}/{len(groups)}] {case}/{variant}: {prop_allowlist}",
            file=sys.stderr,
        )
        returncode, stdout, stderr, timed_out = run_command_group(
            cmd,
            cwd=REPO_ROOT,
            env=_default_cbmc_env(),
            timeout=args.target_timeout_per_case,
        )
        log_path.write_text(
            "COMMAND: " + " ".join(cmd) + "\n\n"
            + (f"TIMED OUT after {args.target_timeout_per_case}s\n\n" if timed_out else "")
            + "STDOUT:\n" + stdout + "\n\n"
            + "STDERR:\n" + stderr,
            encoding="utf-8",
        )
        error = (
            f"timeout after {args.target_timeout_per_case}s"
            if timed_out
            else (None if returncode == 0 else f"exit {returncode}")
        )
        rows.append(
            {
                "case": case,
                "variant": variant,
                "package": str(package),
                "output": str(case_output),
                "log": str(log_path),
                "returncode": returncode,
                "timed_out": timed_out,
                "error": error,
                "selected_properties": group.get("properties") or [],
                "selected_functions": group.get("functions") or [],
                "run": collect_run_summary(case_output) if case_output.exists() else {},
            }
        )
        (target_dir / "targeted_batch_summary.json").write_text(
            json.dumps(batch_aggregate(rows, start), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    summary = batch_aggregate(rows, start)
    summary["selected_groups"] = groups
    summary["allow_targeted_llm"] = True
    (target_dir / "targeted_batch_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary, (0 if all(row.get("returncode") == 0 for row in rows) else 1)


def render_targeted_pipeline_markdown(summary: dict[str, Any]) -> str:
    plan = summary.get("targeted_plan") or {}
    scan_report = summary.get("scan_report") or {}
    targeted = summary.get("targeted_summary") or {}
    gate = summary.get("api_spend_gate") or {}
    lines = [
        "# Candidate-First Targeted Realism Pipeline",
        "",
        f"Status: `{summary.get('status')}`",
        f"Duration: {summary.get('duration_sec')} sec",
        f"Targeted LLM allowed: `{summary.get('allow_targeted_llm')}`",
        f"Candidate selection mode: `{summary.get('candidate_selection_mode')}`",
        "",
        "## Artifacts",
        "",
        f"- scan batch: `{summary.get('scan_batch_summary')}`",
        f"- scan report: `{summary.get('scan_run_report')}`",
        f"- triage: `{summary.get('triage')}`",
        f"- targeted plan: `{summary.get('targeted_plan_json')}`",
        f"- targeted batch: `{summary.get('targeted_batch_summary')}`",
        f"- targeted report: `{summary.get('targeted_run_report')}`",
        "",
        "## Cheap Scan",
        "",
        f"- status: `{scan_report.get('status')}`",
        f"- variants run: {(scan_report.get('batch') or {}).get('total_cases')}",
        f"- CBMC errors: {((scan_report.get('batch') or {}).get('cbmc') or {}).get('errors')}",
        f"- API/LLM markers: {(scan_report.get('api_marker_scan') or {}).get('marker_count')}",
        "",
        "## API Spend Gate",
        "",
        f"- decision: `{gate.get('decision')}`",
        f"- eligible for targeted LLM: `{gate.get('eligible_for_targeted_llm')}`",
        f"- estimated LLM calls: {gate.get('estimated_llm_calls')}",
        f"- next action: {gate.get('next_action')}",
        "",
        "### Blockers",
        "",
    ]
    blockers = gate.get("blockers") or []
    if blockers:
        for reason in blockers:
            lines.append(f"- {reason}")
    else:
        lines.append("- none")
    warnings = gate.get("warnings") or []
    if warnings:
        lines.extend(["", "### Warnings", ""])
        for reason in warnings:
            lines.append(f"- {reason}")
    lines.extend([
        "",
        "## Targeted Realism",
        "",
        f"- selected candidates: {plan.get('selected_count')}",
        f"- rerun groups: {len(plan.get('groups') or [])}",
        f"- targeted variants run: {targeted.get('total_cases')}",
        f"- targeted reports: {targeted.get('reports')}",
        "",
        "## Selected Groups",
        "",
        "| case | variant | properties | functions |",
        "|---|---|---|---|",
    ])
    for group in plan.get("groups") or []:
        props = "<br>".join(group.get("properties") or [])
        funcs = "<br>".join(group.get("functions") or [])
        lines.append(f"| {group.get('case')} | {group.get('variant')} | {props} | {funcs} |")
    if not plan.get("groups"):
        lines.append("| | | | |")
    lines.append("")
    return "\n".join(lines)


def cmd_targeted_pipeline(args: argparse.Namespace) -> int:
    """Run cheap scan -> triage -> optional selected-property realism reruns."""
    out_dir = Path(args.output).resolve()
    scan_dir = out_dir / "scan"
    triage_dir = out_dir / "triage"
    plan_dir = out_dir / "targeted_plan"
    scan_report_dir = out_dir / "scan_report"
    out_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    scan_args = argparse.Namespace(
        root=args.root,
        output=str(scan_dir),
        variants=args.variants,
        cases=args.cases,
        limit=args.limit,
        timeout_per_case=args.scan_timeout_per_case,
        minimal=True,
        lite_mode=False,
        no_realism_check=False,
        phase3_property_allowlist="",
        threat_model=args.threat_model,
        max_public=args.max_public,
        max_entrypoints=args.max_entrypoints,
        entrypoint_policy=args.entrypoint_policy,
        check_scope=args.check_scope,
        max_functions=args.max_functions,
        lifecycle_harness=args.lifecycle_harness,
        require_clean_audit=not args.allow_dirty_packages,
        no_suffix_fallback=args.no_suffix_fallback,
    )
    scan_stdout = out_dir / "scan_stdout.log"
    with scan_stdout.open("w", encoding="utf-8") as stdout_log, contextlib.redirect_stdout(stdout_log):
        scan_rc = cmd_batch(scan_args)

    scan_summary = scan_dir / "batch_summary.json"
    if not scan_summary.exists():
        raise SystemExit(f"scan did not produce {scan_summary}")

    triage_dir.mkdir(parents=True, exist_ok=True)
    triage = triage_batch(
        scan_summary,
        max_candidates=args.max_candidates,
        min_score=args.min_score,
        candidate_selection_mode=args.candidate_selection_mode,
    )
    triage_json = triage_dir / "candidate_triage.json"
    triage_md = triage_dir / "candidate_triage.md"
    triage_json.write_text(json.dumps(triage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    triage_md.write_text(render_triage_markdown(triage) + "\n", encoding="utf-8")

    audit_json = scan_dir / "preflight_package_audit" / "package_audit.json"
    scan_report_dir.mkdir(parents=True, exist_ok=True)
    scan_report = build_report(
        batch_summary_path=scan_summary,
        triage_path=triage_json,
        audit_path=audit_json if audit_json.exists() else None,
        logs_dir=scan_dir / "logs",
    )
    scan_report_json = scan_report_dir / "run_report.json"
    scan_report_md = scan_report_dir / "run_report.md"
    scan_report_json.write_text(json.dumps(scan_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    scan_report_md.write_text(render_report_markdown(scan_report), encoding="utf-8")

    plan = write_targeted_plan(triage_json, plan_dir)
    api_spend_gate = evaluate_api_spend_gate(
        scan_report=scan_report,
        triage=triage,
        plan=plan,
        allow_targeted_llm=bool(args.allow_targeted_llm),
    )
    groups = plan.get("groups") or []
    targeted_summary: dict[str, Any] | None = None
    targeted_report: dict[str, Any] | None = None
    targeted_summary_path: Path | None = None
    targeted_report_json: Path | None = None
    targeted_report_md: Path | None = None
    targeted_rc = 0

    if args.allow_targeted_llm and groups:
        targeted_summary, targeted_rc = run_targeted_realism_groups(args, groups)
        targeted_summary_path = out_dir / "targeted_realism" / "targeted_batch_summary.json"
        targeted_report_dir = out_dir / "targeted_report"
        targeted_report_dir.mkdir(parents=True, exist_ok=True)
        targeted_report = build_report(
            batch_summary_path=targeted_summary_path,
            triage_path=triage_json,
            audit_path=audit_json if audit_json.exists() else None,
            logs_dir=out_dir / "targeted_realism" / "logs",
        )
        targeted_report_json = targeted_report_dir / "run_report.json"
        targeted_report_md = targeted_report_dir / "run_report.md"
        targeted_report_json.write_text(
            json.dumps(targeted_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        targeted_report_md.write_text(render_report_markdown(targeted_report), encoding="utf-8")

    status = "ready"
    if scan_rc != 0 or scan_report.get("status") != "ready":
        status = "scan_issues"
    elif not groups:
        status = "no_candidates"
    elif not args.allow_targeted_llm:
        status = "targeted_realism_planned"
    elif targeted_rc != 0:
        status = "targeted_run_issues"

    summary = {
        "status": status,
        "duration_sec": round(time.time() - start, 3),
        "allow_targeted_llm": bool(args.allow_targeted_llm),
        "candidate_selection_mode": triage.get("candidate_selection_mode"),
        "candidate_selection_mode_requested": triage.get("candidate_selection_mode_requested"),
        "scan_returncode": scan_rc,
        "targeted_returncode": targeted_rc if args.allow_targeted_llm and groups else None,
        "scan_stdout": str(scan_stdout),
        "scan_batch_summary": str(scan_summary),
        "scan_run_report": str(scan_report_json),
        "scan_report": scan_report,
        "package_audit": str(audit_json) if audit_json.exists() else None,
        "triage": str(triage_json),
        "targeted_plan_json": str(plan_dir / "targeted_realism_plan.json"),
        "targeted_plan_md": str(plan_dir / "targeted_realism_plan.md"),
        "targeted_plan": plan,
        "api_spend_gate": api_spend_gate,
        "targeted_batch_summary": str(targeted_summary_path) if targeted_summary_path else None,
        "targeted_run_report": str(targeted_report_json) if targeted_report_json else None,
        "targeted_summary": targeted_summary,
        "targeted_report": targeted_report,
    }
    summary_json = out_dir / "targeted_pipeline_summary.json"
    summary_md = out_dir / "targeted_pipeline_summary.md"
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_md.write_text(render_targeted_pipeline_markdown(summary), encoding="utf-8")
    print(json.dumps({
        "status": status,
        "summary": str(summary_json),
        "markdown": str(summary_md),
        "triage": str(triage_json),
        "targeted_plan": str(plan_dir / "targeted_realism_plan.md"),
        "api_spend_gate": api_spend_gate,
        "targeted_batch": str(targeted_summary_path) if targeted_summary_path else None,
        "selected": plan.get("selected_count"),
        "groups": len(groups),
    }, indent=2, sort_keys=True))
    return 0 if status in {"ready", "no_candidates", "targeted_realism_planned"} else 1


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Run audit -> batch -> triage -> report with low-API defaults."""
    out_dir = Path(args.output).resolve()
    batch_dir = out_dir / "batch"
    triage_dir = out_dir / "triage"
    report_dir = out_dir / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    batch_args = argparse.Namespace(
        root=args.root,
        output=str(batch_dir),
        variants=args.variants,
        cases=args.cases,
        limit=args.limit,
        timeout_per_case=args.timeout_per_case,
        minimal=not args.allow_llm,
        lite_mode=args.lite_mode,
        no_realism_check=args.no_realism_check,
        phase3_property_allowlist=args.phase3_property_allowlist,
        threat_model=args.threat_model,
        max_public=args.max_public,
        max_entrypoints=args.max_entrypoints,
        entrypoint_policy=args.entrypoint_policy,
        check_scope=args.check_scope,
        max_functions=args.max_functions,
        lifecycle_harness=args.lifecycle_harness,
        require_clean_audit=not args.allow_dirty_packages,
        no_suffix_fallback=args.no_suffix_fallback,
    )
    batch_stdout = out_dir / "batch_stdout.log"
    with batch_stdout.open("w", encoding="utf-8") as stdout_log, contextlib.redirect_stdout(stdout_log):
        batch_rc = cmd_batch(batch_args)

    batch_summary_path = batch_dir / "batch_summary.json"
    if not batch_summary_path.exists():
        raise SystemExit(f"batch did not produce {batch_summary_path}")

    triage_dir.mkdir(parents=True, exist_ok=True)
    triage = triage_batch(
        batch_summary_path,
        max_candidates=args.max_candidates,
        min_score=args.min_score,
        candidate_selection_mode=args.candidate_selection_mode,
    )
    triage_json = triage_dir / "candidate_triage.json"
    triage_md = triage_dir / "candidate_triage.md"
    triage_json.write_text(json.dumps(triage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    triage_md.write_text(render_triage_markdown(triage) + "\n", encoding="utf-8")

    audit_json = batch_dir / "preflight_package_audit" / "package_audit.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(
        batch_summary_path=batch_summary_path,
        triage_path=triage_json,
        audit_path=audit_json if audit_json.exists() else None,
        logs_dir=batch_dir / "logs",
    )
    report_json = report_dir / "run_report.json"
    report_md = report_dir / "run_report.md"
    report_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_md.write_text(render_report_markdown(report), encoding="utf-8")

    status = report.get("status")
    if batch_rc != 0 and status == "ready":
        status = "run_issues"
    summary = {
        "status": status,
        "allow_llm": bool(args.allow_llm),
        "duration_sec": round(time.time() - start, 3),
        "batch_returncode": batch_rc,
        "batch_stdout": str(batch_stdout),
        "batch_summary": str(batch_summary_path),
        "package_audit": str(audit_json) if audit_json.exists() else None,
        "triage": str(triage_json),
        "run_report": str(report_json),
        "report": report,
    }
    summary_json = out_dir / "pipeline_summary.json"
    summary_md = out_dir / "pipeline_summary.md"
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_md.write_text(render_pipeline_markdown(summary), encoding="utf-8")

    print(json.dumps({
        "status": status,
        "summary": str(summary_json),
        "markdown": str(summary_md),
        "run_report": str(report_md),
        "api_markers": (report.get("api_marker_scan") or {}).get("marker_count"),
        "selected": (report.get("triage_summary") or {}).get("selected_count"),
    }, indent=2, sort_keys=True))
    return 0 if status == "ready" else 1


def cmd_discover(args: argparse.Namespace) -> int:
    discovery = discover(
        Path(args.package),
        include_suffix_fallback=not args.no_suffix_fallback,
        max_public=args.max_public,
    )
    payload = build_manifest(discovery)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    discovery = discover(
        Path(args.package),
        include_suffix_fallback=not args.no_suffix_fallback,
        max_public=args.max_public,
    )
    return run_bmc(discovery, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    discover_p = sub.add_parser("discover", help="discover driver entrypoints and kernel external symbols")
    discover_p.add_argument("--package", required=True, help="prepared slice package directory")
    discover_p.add_argument("--output", default="", help="optional JSON output path")
    discover_p.add_argument("--max-public", type=int, default=12, help="cap public-function fallback entrypoints")
    discover_p.add_argument("--no-suffix-fallback", action="store_true", help="disable name-suffix fallback")
    discover_p.set_defaults(func=cmd_discover)

    run_p = sub.add_parser("run", help="run BMC-Agent with kernel-aware driver scope")
    run_p.add_argument("--package", required=True, help="prepared slice package directory")
    run_p.add_argument("--output", required=True, help="artifact output directory")
    run_p.add_argument("--driver", default="", help="logical driver name")
    run_p.add_argument("--minimal", action="store_true", help="zero-LLM smoke mode")
    run_p.add_argument(
        "--lite-mode",
        action="store_true",
        help="low-cost mode: skip spec-generation LLM and non-Phase-3 LLM helpers",
    )
    run_p.add_argument("--no-dynamic-validation", action="store_true", default=True)
    run_p.add_argument("--no-realism-check", action="store_true", default=False)
    run_p.add_argument(
        "--phase3-property-allowlist",
        default="",
        help="comma-separated exact failing_property values to validate in Phase 3",
    )
    run_p.add_argument("--threat-model", default="security", choices=["security", "safety", "functional"])
    run_p.add_argument("--max-public", type=int, default=12, help="cap public-function fallback entrypoints")
    run_p.add_argument("--max-entrypoints", type=int, default=None, help="optional cap after ranking")
    run_p.add_argument(
        "--entrypoint-policy",
        default="ranked",
        choices=["ranked", "lifecycle", "all"],
        help="how to select discovered entrypoints before applying --max-entrypoints",
    )
    run_p.add_argument(
        "--check-scope",
        default="entrypoints",
        choices=["entrypoints", "closure"],
        help="verify selected entrypoints only, or also their driver-local callees",
    )
    run_p.add_argument("--max-functions", type=int, default=None, help="optional cap after check-scope expansion")
    run_p.add_argument(
        "--lifecycle-harness",
        action="store_true",
        help="generate generic probe-success-then-teardown harnesses from callback tables",
    )
    run_p.add_argument("--no-suffix-fallback", action="store_true", help="disable name-suffix fallback")
    run_p.add_argument("--dry-run", action="store_true", help="write/print manifest without running BMC-Agent")
    run_p.set_defaults(func=cmd_run)

    batch_p = sub.add_parser("batch", help="run kernel-aware mode over prepared package variants")
    batch_p.add_argument(
        "--root",
        default="artifacts/linux_driver_cves/slices_remaining",
        help="root containing <case>/<variant> prepared packages",
    )
    batch_p.add_argument("--output", required=True, help="batch artifact output directory")
    batch_p.add_argument("--variants", default="buggy", help="comma-separated variants, e.g. buggy,fixed")
    batch_p.add_argument("--cases", default="", help="optional comma-separated case directory names")
    batch_p.add_argument("--limit", type=int, default=None, help="optional cap after sorting packages")
    batch_p.add_argument("--timeout-per-case", type=int, default=300, help="subprocess timeout per package")
    batch_p.add_argument("--minimal", action="store_true", help="zero-LLM smoke mode")
    batch_p.add_argument(
        "--lite-mode",
        action="store_true",
        help="low-cost mode: skip spec-generation LLM and non-Phase-3 LLM helpers",
    )
    batch_p.add_argument("--no-realism-check", action="store_true", default=False)
    batch_p.add_argument(
        "--phase3-property-allowlist",
        default="",
        help="comma-separated exact failing_property values to validate in Phase 3",
    )
    batch_p.add_argument("--threat-model", default="security", choices=["security", "safety", "functional"])
    batch_p.add_argument("--max-public", type=int, default=12, help="cap public-function fallback entrypoints")
    batch_p.add_argument("--max-entrypoints", type=int, default=None, help="optional cap after ranking")
    batch_p.add_argument(
        "--entrypoint-policy",
        default="ranked",
        choices=["ranked", "lifecycle", "all"],
        help="how to select discovered entrypoints before applying --max-entrypoints",
    )
    batch_p.add_argument(
        "--check-scope",
        default="entrypoints",
        choices=["entrypoints", "closure"],
        help="verify selected entrypoints only, or also their driver-local callees",
    )
    batch_p.add_argument("--max-functions", type=int, default=None, help="optional cap after check-scope expansion")
    batch_p.add_argument(
        "--lifecycle-harness",
        action="store_true",
        help="generate generic probe-success-then-teardown harnesses from callback tables",
    )
    batch_p.add_argument(
        "--require-clean-audit",
        action="store_true",
        help="run package audit first and stop before CBMC if inputs are invalid",
    )
    batch_p.add_argument("--no-suffix-fallback", action="store_true", help="disable name-suffix fallback")
    batch_p.set_defaults(func=cmd_batch)

    triage_p = sub.add_parser("triage", help="rank CBMC counterexamples from an existing batch run")
    triage_p.add_argument("--batch-summary", required=True, help="path to batch_summary.json")
    triage_p.add_argument("--output", required=True, help="directory for candidate_triage.{json,md}")
    triage_p.add_argument("--max-candidates", type=int, default=20, help="maximum candidates to include in report")
    triage_p.add_argument("--min-score", type=int, default=50, help="minimum score for API-worthy candidates")
    triage_p.add_argument(
        "--candidate-selection-mode",
        default="auto",
        choices=["auto", "paired", "unknown"],
        help="auto/paired uses buggy-vs-fixed controls when available; unknown ranks one-source candidates without a fixed oracle",
    )
    triage_p.set_defaults(func=cmd_triage)

    audit_p = sub.add_parser("audit-packages", help="audit prepared packages before running BMC-Agent")
    audit_p.add_argument(
        "--root",
        default="artifacts/linux_driver_cves/slices_remaining",
        help="root containing <case>/<variant> prepared packages",
    )
    audit_p.add_argument("--variants", default="buggy,fixed", help="comma-separated variants to audit")
    audit_p.add_argument("--cases", default="", help="optional comma-separated case directory names")
    audit_p.add_argument("--output", default="", help="optional directory for package_audit.{json,md}")
    audit_p.add_argument("--strict", action="store_true", help="exit non-zero when audit issues are found")
    audit_p.set_defaults(func=cmd_audit_packages)

    report_p = sub.add_parser("report", help="compose audit, batch, triage, and log checks into one run report")
    report_p.add_argument("--batch-summary", required=True, help="path to batch_summary.json")
    report_p.add_argument("--triage", default="", help="optional candidate_triage.json")
    report_p.add_argument("--audit", default="", help="optional package_audit.json")
    report_p.add_argument("--logs-dir", default="", help="optional logs directory; defaults beside batch_summary")
    report_p.add_argument("--output", required=True, help="directory for run_report.{json,md}")
    report_p.set_defaults(func=cmd_report)

    pipeline_p = sub.add_parser("pipeline", help="run audit, batch, triage, and report with low-API defaults")
    pipeline_p.add_argument(
        "--root",
        default="artifacts/linux_driver_cves/slices_remaining",
        help="root containing <case>/<variant> prepared packages",
    )
    pipeline_p.add_argument("--output", required=True, help="pipeline artifact output directory")
    pipeline_p.add_argument("--variants", default="buggy,fixed", help="comma-separated variants, e.g. buggy,fixed")
    pipeline_p.add_argument("--cases", default="", help="optional comma-separated case directory names")
    pipeline_p.add_argument("--limit", type=int, default=None, help="optional cap after sorting packages")
    pipeline_p.add_argument("--timeout-per-case", type=int, default=300, help="subprocess timeout per package")
    pipeline_p.add_argument(
        "--allow-llm",
        action="store_true",
        help="allow BMC-Agent LLM stages; by default pipeline runs minimal/no-external-LLM mode",
    )
    pipeline_p.add_argument("--lite-mode", action="store_true", help="skip spec-generation LLM when --allow-llm is used")
    pipeline_p.add_argument("--no-realism-check", action="store_true", default=False)
    pipeline_p.add_argument(
        "--phase3-property-allowlist",
        default="",
        help="comma-separated exact failing_property values to validate in Phase 3",
    )
    pipeline_p.add_argument("--threat-model", default="security", choices=["security", "safety", "functional"])
    pipeline_p.add_argument("--max-public", type=int, default=12, help="cap public-function fallback entrypoints")
    pipeline_p.add_argument("--max-entrypoints", type=int, default=2, help="optional cap after ranking")
    pipeline_p.add_argument(
        "--entrypoint-policy",
        default="lifecycle",
        choices=["ranked", "lifecycle", "all"],
        help="how to select discovered entrypoints before applying --max-entrypoints",
    )
    pipeline_p.add_argument(
        "--check-scope",
        default="entrypoints",
        choices=["entrypoints", "closure"],
        help="verify selected entrypoints only, or also their driver-local callees",
    )
    pipeline_p.add_argument("--max-functions", type=int, default=1, help="optional cap after check-scope expansion")
    pipeline_p.add_argument(
        "--lifecycle-harness",
        action="store_true",
        default=True,
        help="generate generic probe-success-then-teardown harnesses from callback tables",
    )
    pipeline_p.add_argument(
        "--no-lifecycle-harness",
        dest="lifecycle_harness",
        action="store_false",
        help="disable generated lifecycle harnesses",
    )
    pipeline_p.add_argument(
        "--allow-dirty-packages",
        action="store_true",
        help="do not fail preflight when package audit reports input issues",
    )
    pipeline_p.add_argument("--no-suffix-fallback", action="store_true", help="disable name-suffix fallback")
    pipeline_p.add_argument("--max-candidates", type=int, default=20, help="maximum candidates to include in triage")
    pipeline_p.add_argument("--min-score", type=int, default=50, help="minimum score for selected triage candidates")
    pipeline_p.add_argument(
        "--candidate-selection-mode",
        default="auto",
        choices=["auto", "paired", "unknown"],
        help="auto/paired uses buggy-vs-fixed controls when available; unknown ranks one-source candidates without a fixed oracle",
    )
    pipeline_p.set_defaults(func=cmd_pipeline)

    targeted_p = sub.add_parser(
        "targeted-pipeline",
        help="run minimal scan, triage, and optional selected-property realism reruns",
    )
    targeted_p.add_argument(
        "--root",
        default="artifacts/linux_driver_cves/slices_remaining",
        help="root containing <case>/<variant> prepared packages",
    )
    targeted_p.add_argument("--output", required=True, help="targeted pipeline artifact output directory")
    targeted_p.add_argument("--variants", default="buggy,fixed", help="comma-separated variants, e.g. buggy,fixed")
    targeted_p.add_argument("--cases", default="", help="optional comma-separated case directory names")
    targeted_p.add_argument("--limit", type=int, default=None, help="optional cap after sorting packages")
    targeted_p.add_argument("--scan-timeout-per-case", type=int, default=300, help="minimal scan timeout per package")
    targeted_p.add_argument("--target-timeout-per-case", type=int, default=600, help="targeted realism timeout per package")
    targeted_p.add_argument(
        "--allow-targeted-llm",
        action="store_true",
        help="actually run selected-property realism with external LLM stages",
    )
    targeted_p.add_argument(
        "--targeted-lite-mode",
        action="store_true",
        help="skip spec-generation and non-Phase-3 LLM helpers in targeted reruns while keeping Phase 3 enabled",
    )
    targeted_p.add_argument("--threat-model", default="security", choices=["security", "safety", "functional"])
    targeted_p.add_argument("--max-public", type=int, default=12, help="cap public-function fallback entrypoints")
    targeted_p.add_argument("--max-entrypoints", type=int, default=2, help="optional cap after ranking")
    targeted_p.add_argument(
        "--entrypoint-policy",
        default="lifecycle",
        choices=["ranked", "lifecycle", "all"],
        help="how to select discovered entrypoints before applying --max-entrypoints",
    )
    targeted_p.add_argument(
        "--check-scope",
        default="entrypoints",
        choices=["entrypoints", "closure"],
        help="verify selected entrypoints only, or also their driver-local callees",
    )
    targeted_p.add_argument("--max-functions", type=int, default=1, help="optional cap after check-scope expansion")
    targeted_p.add_argument(
        "--lifecycle-harness",
        action="store_true",
        default=True,
        help="generate generic probe-success-then-teardown harnesses from callback tables",
    )
    targeted_p.add_argument(
        "--no-lifecycle-harness",
        dest="lifecycle_harness",
        action="store_false",
        help="disable generated lifecycle harnesses",
    )
    targeted_p.add_argument(
        "--allow-dirty-packages",
        action="store_true",
        help="do not fail preflight when package audit reports input issues",
    )
    targeted_p.add_argument("--no-suffix-fallback", action="store_true", help="disable name-suffix fallback")
    targeted_p.add_argument("--max-candidates", type=int, default=20, help="maximum candidates to include in triage")
    targeted_p.add_argument("--min-score", type=int, default=50, help="minimum score for selected triage candidates")
    targeted_p.add_argument(
        "--candidate-selection-mode",
        default="auto",
        choices=["auto", "paired", "unknown"],
        help="auto/paired uses buggy-vs-fixed controls when available; unknown ranks one-source candidates without a fixed oracle",
    )
    targeted_p.set_defaults(func=cmd_targeted_pipeline)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
