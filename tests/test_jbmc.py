"""Tests for Java/JBMC support.

The subprocess tests mock ``javac`` and ``jbmc`` so they can run on developer
machines that do not have the CProver Java toolchain installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from bmc_agent.cbmc import CBMCResult
from bmc_agent.jbmc import (
    build_jbmc_command,
    normalize_java_entry,
    run_jbmc,
    _parse_jbmc_output,
)


def test_normalize_java_entry_qualifies_simple_method():
    assert normalize_java_entry("main", "Example") == "Example"
    assert normalize_java_entry("Example", "Example") == "Example"
    assert normalize_java_entry("Example.main", "Other") == "Example"
    assert normalize_java_entry("check", "Example") == "Example.check"
    assert normalize_java_entry("Example.check", "Other") == "Example.check"
    assert normalize_java_entry("", "Example") == "Example"


def test_build_jbmc_command_shape():
    cmd = build_jbmc_command(
        jbmc_path="jbmc",
        target="Example.main",
        classpath=["/classes", "/lib/a.jar"],
        unwind=7,
        extra_jbmc_args=["--pointer-check"],
    )
    assert cmd[0] == "jbmc"
    assert "--classpath" in cmd
    assert os.pathsep.join(["/classes", "/lib/a.jar"]) in cmd
    assert "--json-ui" in cmd
    assert "--unwind" in cmd and "7" in cmd
    assert "--unwinding-assertions" in cmd
    assert "--pointer-check" in cmd
    assert cmd[1] == "Example.main"


def test_parse_jbmc_output_reuses_cbmc_json_schema():
    raw = json.dumps(
        [
            {"program": "jbmc"},
            {
                "result": [
                    {
                        "status": "FAILURE",
                        "property": {"id": "Example.assertion.1"},
                        "description": "assertion",
                        "trace": [],
                    }
                ]
            },
        ]
    )
    result = _parse_jbmc_output(raw, stderr="", returncode=10)
    assert result.verified is False
    assert len(result.counterexamples) == 1
    assert result.counterexamples[0].failing_property == "Example.assertion.1"


def test_run_jbmc_not_installed_returns_clean_error(tmp_path: Path):
    src = tmp_path / "Example.java"
    src.write_text("public class Example { public static void main(String[] args) {} }\n")
    with patch("bmc_agent.jbmc.shutil.which", return_value=None):
        result = run_jbmc(src)
    assert isinstance(result, CBMCResult)
    assert result.verified is False
    assert result.error == "jbmc not found"


def test_run_jbmc_javac_not_installed_returns_clean_error(tmp_path: Path):
    src = tmp_path / "Example.java"
    src.write_text("public class Example { public static void main(String[] args) {} }\n")

    def fake_which(binary):
        return "/usr/bin/jbmc" if binary == "jbmc" else None

    with patch("bmc_agent.jbmc.shutil.which", side_effect=fake_which):
        result = run_jbmc(src)
    assert result.verified is False
    assert result.error == "javac not found"


def test_run_jbmc_compiles_then_invokes_jbmc(tmp_path: Path):
    src = tmp_path / "Example.java"
    src.write_text(
        "public class Example { public static void main(String[] args) { assert true; } }\n",
        encoding="utf-8",
    )
    build_dir = tmp_path / "classes"
    calls: list[list[str]] = []

    class Done:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_which(binary):
        return f"/usr/bin/{binary}"

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(list(cmd))
        if cmd[0] == "javac":
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "Example.class").write_bytes(b"\xca\xfe\xba\xbe")
            return Done(returncode=0)
        return Done(stdout=json.dumps([{"result": [{"status": "SUCCESS", "property": {"id": "ok"}}]}]), returncode=0)

    with patch("bmc_agent.jbmc.shutil.which", side_effect=fake_which), \
         patch("bmc_agent.jbmc.subprocess.run", side_effect=fake_run):
        result = run_jbmc(
            src,
            entry="main",
            build_dir=build_dir,
            classpath=["/tmp/lib"],
            unwind=9,
            timeout=33,
            compile_timeout=12,
        )

    assert result.verified is True
    javac_cmd, jbmc_cmd = calls
    assert javac_cmd[:4] == ["javac", "-g", "-d", str(build_dir)]
    assert "-classpath" in javac_cmd and "/tmp/lib" in javac_cmd
    assert str(src) in javac_cmd
    assert jbmc_cmd[0] == "jbmc"
    assert "--classpath" in jbmc_cmd
    cp = jbmc_cmd[jbmc_cmd.index("--classpath") + 1]
    assert str(build_dir) in cp and "/tmp/lib" in cp
    assert "--unwind" in jbmc_cmd and "9" in jbmc_cmd
    assert jbmc_cmd[1] == "Example"


def test_run_jbmc_reports_javac_failure(tmp_path: Path):
    src = tmp_path / "Bad.java"
    src.write_text("public class Bad { syntax error }\n", encoding="utf-8")

    class Done:
        stdout = ""
        stderr = "Bad.java:1: error: ';' expected"
        returncode = 1

    with patch("bmc_agent.jbmc.shutil.which", return_value="/usr/bin/tool"), \
         patch("bmc_agent.jbmc.subprocess.run", return_value=Done()):
        result = run_jbmc(src, build_dir=tmp_path / "classes")

    assert result.verified is False
    assert result.error is not None
    assert "javac exited with code 1" in result.error


def test_run_jbmc_reports_javac_without_class_files(tmp_path: Path):
    src = tmp_path / "Example.java"
    src.write_text("public class Example { public static void main(String[] args) {} }\n")

    class Done:
        stdout = "warning only"
        stderr = ""
        returncode = 0

    with patch("bmc_agent.jbmc.shutil.which", return_value="/usr/bin/tool"), \
         patch("bmc_agent.jbmc.subprocess.run", return_value=Done()):
        result = run_jbmc(src, build_dir=tmp_path / "classes")

    assert result.verified is False
    assert result.error is not None
    assert "javac produced no .class files" in result.error
