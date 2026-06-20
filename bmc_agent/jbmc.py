"""JBMC subprocess wrapper for Java support.

JBMC is the Java bytecode front-end in the CProver suite.  The wrapper keeps
the same result shape as :mod:`bmc_agent.cbmc` so downstream reporting can
consume Java verification results without a parallel data model.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from bmc_agent.cbmc import CBMCResult, _parse_cbmc_output
from bmc_agent.java_parser import parse_java_file


def run_jbmc(
    source_path: str | Path,
    entry: str = "main",
    unwind: int = 4,
    timeout: int = 120,
    jbmc_path: str = "jbmc",
    javac_path: str = "javac",
    classpath: list[str] | None = None,
    build_dir: str | Path | None = None,
    compile_timeout: int = 60,
    extra_jbmc_args: list[str] | None = None,
) -> CBMCResult:
    """Compile a Java source file and verify an entry with JBMC.

    Parameters mirror :func:`bmc_agent.cbmc.run_cbmc` where practical.  ``entry``
    may be either a simple method name (``main``) or a qualified JBMC target
    (``Class.method``).  Simple names are qualified with the source file's
    public/primary class.
    """

    source = Path(source_path)
    if not source.exists():
        return CBMCResult(verified=False, error=f"source file not found: {source}")
    if source.suffix.lower() != ".java":
        return CBMCResult(verified=False, error=f"JBMC source must be .java, got: {source}")
    if not shutil.which(jbmc_path):
        return CBMCResult(verified=False, error="jbmc not found")
    if not shutil.which(javac_path):
        return CBMCResult(verified=False, error="javac not found")

    parsed = parse_java_file(source)
    target = normalize_java_entry(entry, parsed.primary_class)

    if build_dir is None:
        with tempfile.TemporaryDirectory(prefix="bmc_agent_jbmc_") as tmp:
            return _compile_and_run(
                source=source,
                target=target,
                build_dir=Path(tmp),
                user_classpath=classpath or [],
                javac_path=javac_path,
                jbmc_path=jbmc_path,
                unwind=unwind,
                timeout=timeout,
                compile_timeout=compile_timeout,
                extra_jbmc_args=extra_jbmc_args or [],
            )
    return _compile_and_run(
        source=source,
        target=target,
        build_dir=Path(build_dir),
        user_classpath=classpath or [],
        javac_path=javac_path,
        jbmc_path=jbmc_path,
        unwind=unwind,
        timeout=timeout,
        compile_timeout=compile_timeout,
        extra_jbmc_args=extra_jbmc_args or [],
    )


def normalize_java_entry(entry: str, primary_class: str) -> str:
    """Return the JBMC target for *entry*.

    For Java ``main``, JBMC expects the class name and then automatically uses
    ``public static void main(String[])``.  Non-main method entries are returned
    as qualified method targets; callers that need overload disambiguation can
    pass JBMC's full method form directly, including the JVM descriptor.
    """

    e = (entry or "main").strip()
    if e == "main":
        return primary_class
    if e == primary_class:
        return e
    if e.endswith(".main"):
        return e.rsplit(".", 1)[0]
    if "." in e:
        return e
    return f"{primary_class}.{e}"


def build_jbmc_command(
    *,
    jbmc_path: str,
    target: str,
    classpath: list[str],
    unwind: int,
    extra_jbmc_args: list[str] | None = None,
) -> list[str]:
    """Build the JBMC command line in one place for tests and logging."""

    cp = os.pathsep.join(classpath)
    cmd = [
        jbmc_path,
        target,
        "--classpath",
        cp,
        "--json-ui",
        "--unwind",
        str(unwind),
        "--unwinding-assertions",
    ]
    cmd.extend(extra_jbmc_args or [])
    return cmd


def _compile_and_run(
    *,
    source: Path,
    target: str,
    build_dir: Path,
    user_classpath: list[str],
    javac_path: str,
    jbmc_path: str,
    unwind: int,
    timeout: int,
    compile_timeout: int,
    extra_jbmc_args: list[str],
) -> CBMCResult:
    build_dir.mkdir(parents=True, exist_ok=True)
    javac_cmd = [javac_path, "-g", "-d", str(build_dir)]
    if user_classpath:
        javac_cmd += ["-classpath", os.pathsep.join(user_classpath)]
    javac_cmd.append(str(source))

    try:
        javac_proc = subprocess.run(
            javac_cmd,
            capture_output=True,
            text=True,
            timeout=compile_timeout,
        )
    except subprocess.TimeoutExpired:
        return CBMCResult(verified=False, error=f"javac timed out after {compile_timeout}s")
    except FileNotFoundError:
        return CBMCResult(verified=False, error="javac not found")
    except OSError as exc:
        return CBMCResult(verified=False, error=f"javac OS error: {exc}")
    if javac_proc.returncode != 0:
        msg = (javac_proc.stderr or javac_proc.stdout or "").strip()
        return CBMCResult(
            verified=False,
            raw_output=javac_proc.stdout or "",
            error=f"javac exited with code {javac_proc.returncode}: {msg[:1500]}",
        )
    if not any(build_dir.rglob("*.class")):
        msg = (javac_proc.stderr or javac_proc.stdout or "").strip()
        detail = f": {msg[:1500]}" if msg else ""
        return CBMCResult(
            verified=False,
            raw_output=javac_proc.stdout or "",
            error=f"javac produced no .class files{detail}",
        )

    cp = [str(build_dir)] + list(user_classpath)
    jbmc_cmd = build_jbmc_command(
        jbmc_path=jbmc_path,
        target=target,
        classpath=cp,
        unwind=unwind,
        extra_jbmc_args=extra_jbmc_args,
    )
    try:
        proc = subprocess.run(
            jbmc_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CBMCResult(verified=False, error=f"jbmc timed out after {timeout}s")
    except FileNotFoundError:
        return CBMCResult(verified=False, error="jbmc not found")
    except OSError as exc:
        return CBMCResult(verified=False, error=f"jbmc OS error: {exc}")

    return _parse_jbmc_output(proc.stdout or "", proc.stderr or "", proc.returncode)


def _parse_jbmc_output(raw: str, stderr: str, returncode: int) -> CBMCResult:
    """Parse JBMC JSON-UI output.

    JBMC uses CProver's JSON-UI result schema, so the CBMC parser is the
    authoritative implementation.
    """

    return _parse_cbmc_output(raw, stderr, returncode)
