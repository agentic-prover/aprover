"""Tests for Java/JML specs-bench support."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bmc_agent.jml_specs import (
    build_openjml_command,
    count_jml_clauses,
    extract_java_source,
    normalize_jml_annotation_placement,
    run_openjml,
    source_code_preserved,
    strip_jml_comments,
)


def test_extract_java_source_prefers_fenced_code():
    reply = "Here is the code:\n```java\npublic class X {}\n```\n"
    assert extract_java_source(reply) == "public class X {}"
    assert extract_java_source("public class Y {}") == "public class Y {}"


def test_strip_jml_comments_removes_line_and_block_annotations():
    annotated = """
public class X {
  /*@ spec_public @*/ private int value;
  //@ ensures \\result == x + 1;
  public int inc(int x) {
    //@ maintaining i >= 0;
    for (int i = 0; i < 1; i++) {}
    return x + 1;
  }
}
"""
    stripped = strip_jml_comments(annotated)
    assert "ensures" not in stripped
    assert "maintaining" not in stripped
    assert "spec_public" not in stripped
    assert "return x + 1" in stripped


def test_source_code_preserved_allows_only_jml_insertions():
    original = """
public class X {
  public int inc(int x) { return x + 1; }
}
"""
    annotated = """
public class X {
  //@ ensures \\result == x + 1;
  public int inc(int x) { return x + 1; }
}
"""
    changed = """
public class X {
  //@ ensures \\result == x + 2;
  public int inc(int x) { return x + 2; }
}
"""
    ok, err = source_code_preserved(original, annotated)
    assert ok and err == ""
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_java_formatting_only():
    original = """
public class X {
  int f(int n) { return n-1; }
}
"""
    reformatted = """
public class X {
  //@ ensures \\result == n - 1;
  int f(int n) {
    return n - 1;
  }
}
"""
    changed = """
public class X {
  int f(int n) {
    int tmp = n - 1;
    return tmp;
  }
}
"""
    ok, err = source_code_preserved(original, reformatted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "token" in err


def test_count_jml_clauses():
    src = """
//@ requires x >= 0;
//@ ensures \\result >= 0;
//@ assignable \\nothing;
//@ maintaining i >= 0;
//@ decreases n - i;
"""
    counts = count_jml_clauses(src)
    assert counts["requires"] == 1
    assert counts["ensures"] == 1
    assert counts["assignable"] == 1
    assert counts["maintaining"] == 1
    assert counts["decreases"] == 1
    assert counts["total"] >= 5


def test_normalize_jml_annotation_placement_moves_comments_only():
    original = """
public class Return100 {
    public static int return100 ()
        //@ ensures \\result == 100;
    {
        int res = 0;
        for(int i = 0; i < 100; i++) {
            //@ maintaining res == i;
            //@ decreasing 100 - i;
            res = res + 1;
        }
        return res;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "    //@ ensures \\result == 100;\n    public static int return100 ()" in normalized
    assert "        //@ maintaining res == i;\n        //@ decreases 100 - i;\n        for(int i = 0; i < 100; i++) {" in normalized
    ok, err = source_code_preserved(strip_jml_comments(original), normalized)
    assert ok, err


def test_normalize_loop_specs_uses_openjml_loop_keywords():
    original = """
public class X {
    //@ assignable \\nothing;
    public int f(int n) {
        int s = 0;
        /*@
          loop_invariant 0 <= i && i <= n;
          loop_variant n - i;
          assignable s;
        @*/
        for (int i = 0; i < n; i++) {
            s += i;
        }
        return s;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "    //@ assignable \\nothing;" in normalized
    assert "maintaining 0 <= i && i <= n;" in normalized
    assert "decreases n - i;" in normalized
    assert "assignable s;" not in normalized


def test_normalize_jml_range_quantifier_shorthand():
    original = """
public class X {
    //@ maintaining s == (\\sum int k; k in 0..i; k);
    //@ decreases n - i;
    public int f(int n) { return n; }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "\\sum int k; 0 <= k && k <= i; k" in normalized
    assert "k in 0..i" not in normalized


def test_normalize_moves_inner_loop_annotations_to_inner_loop():
    original = """
public class X {
    void sort(int[] a) {
        //@ maintaining 0 <= i && i <= a.length;
        //@ decreases a.length - i;
        //@ maintaining 0 <= j && j < a.length - i;
        //@ decreases a.length - j;
        for (int i = 0; i < a.length; i++) {
            for (int j = 0; j < a.length - i; j++) {
                a[j] = a[j];
            }
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    outer = (
        "        //@ maintaining 0 <= i && i <= a.length;\n"
        "        //@ decreases a.length - i;\n"
        "        for (int i = 0; i < a.length; i++) {"
    )
    inner = (
        "            //@ maintaining 0 <= j && j < a.length - i;\n"
        "            //@ decreases a.length - j;\n"
        "            for (int j = 0; j < a.length - i; j++) {"
    )
    assert outer in normalized
    assert inner in normalized


def test_normalize_strips_old_only_in_loop_annotations():
    original = """
public class X {
    //@ ensures \\result == \\old(x) + 1;
    int f(int x) {
        int s = x;
        //@ maintaining s >= \\old(x);
        //@ decreases x - s;
        while (s < x) {
            s++;
        }
        return s;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ ensures \\result == \\old(x) + 1;" in normalized
    assert "//@ maintaining s >= x;" in normalized


def test_build_openjml_command_shape():
    cmd = build_openjml_command("openjml", "X.java", 33)
    assert cmd[0] == "openjml"
    assert "--esc" in cmd
    assert "--prover=cvc4" in cmd
    assert "--timeout" in cmd and "33" in cmd
    assert cmd[-1] == "X.java"


def test_run_openjml_pass_requires_empty_output(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text("public class X {}\n")

    class Done:
        stdout = ""
        stderr = ""
        returncode = 0

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch("bmc_agent.jml_specs.subprocess.run", return_value=Done()):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9)
    assert result.passed is True
    assert result.status == "passed"


def test_run_openjml_nonempty_output_is_verification_failure(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text("public class X {}\n")

    class Done:
        stdout = "X.java:2: verify: The prover cannot establish an assertion"
        stderr = ""
        returncode = 0

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch("bmc_agent.jml_specs.subprocess.run", return_value=Done()):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9)
    assert result.passed is False
    assert result.status == "verification_failed"
