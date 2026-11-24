"""Tests for Java/JML specs-bench support."""

from __future__ import annotations

import subprocess
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bmc_agent.jml_specs import (
    _annotate_reported_nullable,
    _has_reported_jml_annotation_error,
    _has_reported_nullable_failure,
    _prune_reported_annotation_error,
    _prune_reported_assignable,
    _prune_enclosing_loop_specs_for_internal_error,
    _prune_reported_diverges,
    _prune_reported_loop_decreases,
    _prune_reported_loop_invariant,
    _prune_reported_object_invariant,
    _prune_reported_precondition,
    _prune_reported_postcondition,
    _refine_user_prompt,
    _initial_user_prompt,
    _run_process_group,
    abstract_java_constant_null_try_catch_for_openjml,
    abstract_java_constant_string_construction_for_openjml,
    abstract_java_constant_string_methods_for_openjml,
    abstract_java_constant_string_split_for_openjml,
    abstract_java_char_array_slice_equals_for_openjml,
    abstract_java_charsequence_string_alias_for_openjml,
    abstract_java_debug_output_for_openjml,
    abstract_java_dropped_wrapper_conversion_for_openjml,
    abstract_java_impossible_string_affix_equals_for_openjml,
    abstract_java_string_valueof_object_self_concat_for_openjml,
    abstract_java_tochararray_first_char_concat_for_openjml,
    abstract_java_literal_regex_find_for_openjml,
    abstract_java_literal_string_comparison_loops_for_openjml,
    abstract_java_literal_string_array_foreach_for_openjml,
    abstract_java_simple_stringbuilder_for_openjml,
    abstract_java_stringbuilder_getchars_self_compare_for_openjml,
    abstract_java_system_termination_for_openjml,
    abstract_java_verifier_only_effects_for_openjml,
    build_openjml_command,
    complete_standard_imports,
    count_jml_clauses,
    default_openjml_path,
    drop_generated_jml_assertions,
    extract_java_source,
    java_verification_filename,
    kill_active_openjml_process_groups,
    normalize_jml_annotation_placement,
    repair_java_source_for_openjml,
    run_jml_specs_bench,
    run_openjml,
    source_code_preserved,
    source_code_preserved_with_standard_imports,
    strip_jml_comments,
    transplant_jml_annotations,
    write_openjml_support_files,
)


class _FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def complete(self, *args, **kwargs):
        self.calls += 1
        return self.reply


def test_extract_java_source_prefers_fenced_code():
    reply = "Here is the code:\n```java\npublic class X {}\n```\n"
    assert extract_java_source(reply) == "public class X {}"
    assert extract_java_source("public class Y {}") == "public class Y {}"


def test_default_openjml_path_prefers_env(monkeypatch):
    monkeypatch.setenv("BMC_AGENT_OPENJML_PATH", "/custom/openjml")
    monkeypatch.setattr("bmc_agent.jml_specs.shutil.which", lambda _: "/usr/bin/openjml")

    assert default_openjml_path() == "/custom/openjml"


def test_default_openjml_path_uses_path_before_workspace(monkeypatch):
    monkeypatch.delenv("BMC_AGENT_OPENJML_PATH", raising=False)
    monkeypatch.setattr("bmc_agent.jml_specs.shutil.which", lambda _: "/usr/bin/openjml")

    assert default_openjml_path() == "/usr/bin/openjml"


def test_default_openjml_path_discovers_workspace_artifact(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("BMC_AGENT_OPENJML_PATH", raising=False)
    monkeypatch.setattr("bmc_agent.jml_specs.shutil.which", lambda _: None)
    fake_module = tmp_path / "workspace" / "repo" / "bmc_agent" / "jml_specs.py"
    fake_openjml = tmp_path / "workspace" / "SpecGen-Artifact" / "openjml" / "openjml"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("", encoding="utf-8")
    fake_openjml.parent.mkdir(parents=True)
    fake_openjml.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("bmc_agent.jml_specs.__file__", str(fake_module))

    assert default_openjml_path() == str(fake_openjml)


def test_java_verification_filename_uses_public_type_name():
    assert java_verification_filename("public class Main {}\n", "StringStartEnd03.java") == "Main.java"
    assert java_verification_filename("class Helper {}\n", "HelperCase.java") == "HelperCase.java"


def test_java_verification_filename_ignores_nested_public_type():
    source = """
class Environment {
  public enum WaterLevelEnum { low, normal, high }
}

public class MinePump {
}
"""
    assert java_verification_filename(source, "MinePumpCase.java") == "MinePump.java"


def test_repair_java_source_for_openjml_drops_top_level_static_type():
    source = """
static class Node {
  static class Nested {
  }
}
"""
    repaired = repair_java_source_for_openjml(source)
    assert "static class Node" not in repaired
    assert "class Node" in repaired
    assert "static class Nested" in repaired


def test_repair_java_source_for_openjml_preserves_nested_static_type():
    source = """
class Outer {
  static class Nested {
  }
}
"""
    assert repair_java_source_for_openjml(source) == source


def test_repair_java_source_for_openjml_rewrites_renamed_main_alias():
    source = """
public class ExException {
  int zero() { return 0; }
  static int test(int secret) {
    Main o = null;
    o = new Main();
    return o.zero();
  }
}
"""
    repaired = repair_java_source_for_openjml(source)
    assert "Main o = null" not in repaired
    assert "new Main()" not in repaired
    assert "ExException o = null" in repaired
    assert "new ExException()" in repaired


def test_repair_java_source_for_openjml_preserves_declared_main():
    source = """
class Main {}
public class Wrapper {
  Main make() { return new Main(); }
}
"""
    assert repair_java_source_for_openjml(source) == source


def test_repair_java_source_for_openjml_adds_missing_new_for_constructor_call():
    source = """
class Problem {
  boolean checkInvariant() { return true; }
}
public class Lazy {
  public static boolean f() {
    return Problem().checkInvariant();
  }
}
"""
    repaired = repair_java_source_for_openjml(source)
    assert "return new Problem().checkInvariant();" in repaired
    assert "class Problem" in repaired
    assert "new Problem() {" not in repaired


def test_repair_java_source_for_openjml_adds_free_args_field():
    source = """
class Main {
  public static boolean f() {
    return args instanceof Object[];
  }
}
"""
    repaired = repair_java_source_for_openjml(source)
    assert "static String[] args;" in repaired
    assert "return args instanceof Object[];" in repaired


def test_repair_java_source_for_openjml_preserves_declared_args():
    source = """
class Main {
  public static void main(String[] args) {
    System.out.println(args.length);
  }
}
"""
    assert repair_java_source_for_openjml(source) == source


def test_repair_java_source_for_openjml_drops_unreachable_tail_return_after_caught_throw():
    source = """
class A extends Exception {}
class X {
  public static boolean f() {
    A a = new A();
    try {
      throw a;
    } catch (Exception e) {
      return false;
    }
    return true;
  }
}
"""

    repaired = repair_java_source_for_openjml(source)

    assert "return true;" not in repaired
    assert "return false;" in repaired


def test_repair_java_source_for_openjml_keeps_reachable_tail_return():
    source = """
class A extends Exception {}
class X {
  public static boolean f(boolean b) {
    try {
      if (b) throw new A();
    } catch (Exception e) {
      return false;
    }
    return true;
  }
}
"""

    assert repair_java_source_for_openjml(source) == source


def test_repair_java_source_for_openjml_braces_same_line_nested_loops():
    source = """
class X {
  static void f(int[][] a, int x, int y) {
    for (int i = 0; i < x; ++i) for (int j = 0; j < y; ++j) a[i][j] = i + j;
  }
}
"""

    repaired = repair_java_source_for_openjml(source)

    assert "for (int i = 0; i < x; ++i) {" in repaired
    assert "for (int j = 0; j < y; ++j) {" in repaired
    assert "a[i][j] = i + j;" in repaired
    assert repaired.count("{") == repaired.count("}")


def test_repair_java_source_for_openjml_braces_two_line_nested_loops():
    source = """
class X {
  static void f(int[][] a, int x, int y) {
    for (int i = 0; i < x; ++i)
      for (int j = 0; j < y; ++j) a[i][j] = i + j;
  }
}
"""

    repaired = repair_java_source_for_openjml(source)

    assert "for (int i = 0; i < x; ++i) {" in repaired
    assert "for (int j = 0; j < y; ++j) {" in repaired
    assert "a[i][j] = i + j;" in repaired
    assert repaired.count("{") == repaired.count("}")


def test_repair_java_source_for_openjml_leaves_single_loop_unchanged():
    source = """
class X {
  static void f(int[] a, int x) {
    for (int i = 0; i < x; ++i) a[i] = i;
  }
}
"""

    assert repair_java_source_for_openjml(source) == source


def test_repair_java_source_for_openjml_unrolls_tiny_constant_nested_loop():
    source = """
class MatrixAdd {
  int[][] add(int[][] a, int[][] b) {
    int[][] c = new int[2][2];
    for (int i = 0; i < 2; i++) {
      for (int j = 0; j < 2; j++) {
        c[i][j] = a[i][j] + b[i][j];
      }
    }
    return c;
  }
}
"""

    repaired = repair_java_source_for_openjml(source)

    assert "for (int i = 0; i < 2; i++)" not in repaired
    assert "int i = 0;" in repaired
    assert "int i = 1;" in repaired
    assert "int j = 0;" in repaired
    assert "int j = 1;" in repaired
    assert repaired.count("c[i][j] = a[i][j] + b[i][j];") == 4
    assert repaired.count("{") == repaired.count("}")


def test_repair_java_source_for_openjml_keeps_dynamic_nested_loop():
    source = """
class TransposeMatrix {
  int[][] transposeMat(int[][] matrix) {
    int m = matrix.length;
    int n = matrix[0].length;
    int[][] transpose = new int[n][m];
    for (int c = 0; c < n; c++) {
      for (int d = 0; d < m; d++) {
        transpose[c][d] = matrix[d][c];
      }
    }
    return transpose;
  }
}
"""

    assert repair_java_source_for_openjml(source) == source


def test_initial_user_prompt_can_include_examples():
    prompt = _initial_user_prompt(
        "public class T {}",
        "Example input:\n```java\nclass A {}\n```\n\nExample output:\n```java\nclass A {}\n```",
    )
    assert "Here are example Java-to-JML transformations" in prompt
    assert "class A" in prompt
    assert "public class T" in prompt
    assert "array loops" in prompt
    assert "0 <= i && i <= n" in prompt
    assert "--nonnull-by-default" in prompt
    assert "nullable" in prompt
    assert "entry != null" in prompt
    assert "assignable best, visited[*]" in prompt
    assert "a[0]" in prompt
    assert "data > this.x ==> next != null" in prompt


def test_initial_user_prompt_can_include_generation_context():
    prompt = _initial_user_prompt(
        "public class T {}",
        generation_context="Unannotated-source OpenJML status: verification_failed\nFailure reason: Assert",
    )

    assert "Additional verifier context from the unannotated Java source" in prompt
    assert "Failure reason: Assert" in prompt
    assert "Do not hide reachable benchmark assertions" in prompt
    assert "public class T" in prompt


def test_refine_user_prompt_mentions_nullability_guidance():
    prompt = _refine_user_prompt(
        "class X {}",
        "X.java:3: verify: The prover cannot establish an assertion (PossiblyNullInitialization)",
    )
    assert "--nonnull-by-default" in prompt
    assert "nullable" in prompt
    assert "unfaithful non-null invariant" in prompt


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


def test_debug_output_abstraction_preserves_control_tokens():
    source = """
class Main {
  boolean f(boolean b) {
    if (b) System.out.println("yes");
    else System.out.printf("%s", "no");
    if (!b) {
      System.out.print("x");
    } else System.out.println("y");
    return b;
  }
}
"""

    abstracted = abstract_java_debug_output_for_openjml(source)

    assert "System.out" not in abstracted
    assert "if (b) ;" in abstracted
    assert "else ;" in abstracted
    assert "return b;" in abstracted


def test_source_code_preserved_allows_only_debug_output_abstraction():
    original = """
class Main {
  boolean f(boolean b) {
    if (b) System.out.println("yes");
    else System.out.println("no");
    return b;
  }
}
"""
    debug_abstracted = """
class Main {
  boolean f(boolean b) {
    if (b) ;
    else ;
    return b;
  }
}
"""
    changed = """
class Main {
  boolean f(boolean b) {
    if (b) ;
    else ;
    return !b;
  }
}
"""

    ok, err = source_code_preserved(original, debug_abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_system_termination_abstraction_preserves_non_returning_path():
    source = """
class Verifier {
  void assume(boolean condition) {
    if (!condition) Runtime.getRuntime().halt(1);
    if (condition) System.exit(0);
  }
}
"""

    abstracted = abstract_java_system_termination_for_openjml(source)

    assert "Runtime.getRuntime().halt" not in abstracted
    assert "System.exit" not in abstracted
    assert abstracted.count("throw new RuntimeException();") == 2


def test_source_code_preserved_allows_only_system_termination_abstraction():
    original = """
class Verifier {
  boolean assume(boolean condition) {
    if (!condition) Runtime.getRuntime().halt(1);
    return condition;
  }
}
"""
    termination_abstracted = """
class Verifier {
  boolean assume(boolean condition) {
    if (!condition) throw new RuntimeException();
    return condition;
  }
}
"""
    changed = """
class Verifier {
  boolean assume(boolean condition) {
    if (!condition) throw new RuntimeException();
    return true;
  }
}
"""

    ok, err = source_code_preserved(original, termination_abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_verifier_only_effects_composes_debug_and_termination_abstractions():
    source = """
class Main {
  void f(boolean b) {
    System.out.println("x");
    if (!b) Runtime.getRuntime().halt(1);
  }
}
"""

    abstracted = abstract_java_verifier_only_effects_for_openjml(source)

    assert "System.out" not in abstracted
    assert "Runtime.getRuntime().halt" not in abstracted
    assert "throw new RuntimeException();" in abstracted


def test_constant_string_split_abstraction_folds_simple_space_split():
    source = """
class TokenTest {
  boolean f() {
    String sentence = "automatic test case generation";
    String[] tokens = sentence.split(" ");
    return tokens.length == 4;
  }
}
"""

    abstracted = abstract_java_constant_string_split_for_openjml(source)

    assert 'sentence.split(" ")' not in abstracted
    assert 'new String[] {"automatic", "test", "case", "generation"}' in abstracted


def test_constant_string_split_abstraction_matches_java_trailing_empty_rule():
    source = """
class TokenTest {
  boolean f() {
    String sentence = "a b ";
    String[] tokens = sentence.split(" ");
    return tokens.length == 2;
  }
}
"""

    abstracted = abstract_java_constant_string_split_for_openjml(source)

    assert 'new String[] {"a", "b"}' in abstracted


def test_constant_string_construction_abstraction_folds_literal_valueof_and_constructors():
    source = """
class StringConstruction {
  void f() {
    char[] chars = {'d', 'i', 'f', 'f', 'b', 'l', 'u', 'e'};
    Object objectRef = "test";
    boolean ok = false;
    char ch = 'T';
    int i = 7;
    long l = 10000000000L;
    float f = 2.5f;
    String a = String.valueOf(chars);
    String b = String.valueOf(chars, 4, 4);
    String c = String.valueOf(objectRef);
    String d = String.valueOf(ok);
    String e = String.valueOf(ch);
    String g = String.valueOf(i);
    String h = String.valueOf(l);
    String j = String.valueOf(f);
    String k = new String();
    String m = new String("literal");
    String n = new String(chars, 3, 3);
    String o = new String(m);
    String p = new String(chars);
    a = String.valueOf(chars, 0, 3);
  }
}
"""

    abstracted = abstract_java_constant_string_construction_for_openjml(source)

    assert 'String a = "diffblue";' in abstracted
    assert 'String b = "blue";' in abstracted
    assert 'String c = "test";' in abstracted
    assert 'String d = "false";' in abstracted
    assert 'String e = "T";' in abstracted
    assert 'String g = "7";' in abstracted
    assert 'String h = "10000000000";' in abstracted
    assert 'String j = "2.5";' in abstracted
    assert 'String k = "";' in abstracted
    assert 'String m = "literal";' in abstracted
    assert 'String n = "fbl";' in abstracted
    assert 'String o = "literal";' in abstracted
    assert 'String p = "diffblue";' in abstracted
    assert 'a = "dif";' in abstracted


def test_constant_string_construction_abstraction_leaves_input_dependent_calls():
    source = """
class StringConstruction {
  String f(String arg, char[] chars) {
    String a = String.valueOf(arg);
    String b = String.valueOf(chars, 0, 1);
    String c = new String(arg);
    return a + b + c;
  }
}
"""

    abstracted = abstract_java_constant_string_construction_for_openjml(source)

    assert "String.valueOf(arg)" in abstracted
    assert "String.valueOf(chars, 0, 1)" in abstracted
    assert "new String(arg)" in abstracted


def test_constant_string_construction_abstraction_preserves_identity_sensitive_new_string():
    source = """
class StringCompare {
  void f() {
    String s1 = new String("test");
    if (s1 != "test") assert true;
  }
}
"""

    abstracted = abstract_java_constant_string_construction_for_openjml(source)

    assert 'String s1 = new String("test");' in abstracted


def test_constant_string_methods_fold_content_methods_without_identity_rewrite():
    source = """
class StringCompare {
  void f() {
    String s1 = new String("test");
    String s2 = "goodbye";
    String s3 = "Automatic Test Generation";
    String s4 = "automatic test generation";
    if (s1.equals("test")) assert true;
    if (s1 != "test") assert true;
    if (s3.equalsIgnoreCase(s4)) assert true;
    assert s1.compareTo(s2) == 13;
    assert s2.compareTo(s1) == -13;
    assert s3.compareTo(s4) == -32;
    if (!s3.regionMatches(0, s4, 0, 5)) assert true;
    if (s3.regionMatches(true, 0, s4, 0, 5)) assert true;
  }
}
"""

    abstracted = abstract_java_verifier_only_effects_for_openjml(source)

    assert 'String s1 = new String("test");' in abstracted
    assert 's1 != "test"' in abstracted
    assert "if (true) assert true;" in abstracted
    assert "assert 13 == 13;" in abstracted
    assert "assert -13 == -13;" in abstracted
    assert "assert -32 == -32;" in abstracted
    assert "if (!false) assert true;" in abstracted


def test_constant_string_methods_fold_locale_independent_literal_methods():
    source = """
class StringMethods {
  void f() {
    String s = "tested";
    String t = "   automated   ";
    assert s.startsWith("te");
    assert s.startsWith("ste", 2);
    assert s.endsWith("ed");
    assert s.charAt(1) == 'e';
    assert s.length() == 6;
    assert s.indexOf("st") == 2;
    assert s.lastIndexOf('e') == 4;
    String r = "diffblue".replace('f', 'F');
    String u = t.trim();
  }
}
"""

    abstracted = abstract_java_constant_string_methods_for_openjml(source)

    assert "assert true;" in abstracted
    assert "assert 'e' == 'e';" in abstracted
    assert "assert 6 == 6;" in abstracted
    assert "assert 2 == 2;" in abstracted
    assert "assert 4 == 4;" in abstracted
    assert 'String r = "diFFblue";' in abstracted
    assert 'String u = "automated";' in abstracted


def test_constant_string_methods_do_not_fold_locale_or_input_dependent_methods():
    source = """
class StringMethods {
  void f(String arg) {
    String s = "diffblue";
    String upper = s.toUpperCase();
    boolean b = arg.endsWith("ed");
  }
}
"""

    abstracted = abstract_java_constant_string_methods_for_openjml(source)

    assert "toUpperCase()" in abstracted
    assert 'arg.endsWith("ed")' in abstracted


def test_charsequence_string_alias_abstraction_folds_trivial_string_alias():
    source = """
class CharSequenceToString {
  boolean f(String arg) {
    CharSequence cs = (CharSequence) arg;
    String s = cs.toString();
    int i = -1;
    if (s.equals("case1")) i = cs.length();
    return i == -1 || i == 5;
  }
}
"""

    abstracted = abstract_java_charsequence_string_alias_for_openjml(source)

    assert "CharSequence cs" not in abstracted
    assert "String s = arg;" in abstracted
    assert "i = s.length();" in abstracted


def test_charsequence_string_alias_abstraction_requires_string_source_and_length_only_use():
    non_string_source = """
class CharSequenceToString {
  boolean f(Object arg) {
    CharSequence cs = (CharSequence) arg;
    String s = cs.toString();
    return s.length() == 1;
  }
}
"""
    non_length_use = """
class CharSequenceToString {
  boolean f(String arg) {
    CharSequence cs = (CharSequence) arg;
    String s = cs.toString();
    return cs.subSequence(0, 1).equals(s);
  }
}
"""

    assert abstract_java_charsequence_string_alias_for_openjml(non_string_source) == non_string_source
    assert abstract_java_charsequence_string_alias_for_openjml(non_length_use) == non_length_use


def test_literal_regex_find_abstraction_folds_constant_matcher_loop():
    source = r'''
import java.util.regex.Matcher;
import java.util.regex.Pattern;

class RegexMatches {
  boolean f() {
    Pattern expression = Pattern.compile("W.*\\d[0-35-9]-\\d\\d-\\d\\d");
    String string1 =
        "XXXX's Birthday is 05-12-75\n"
            + "YYYY's Birthday is 11-04-68\n"
            + "ZZZZ's Birthday is 04-28-73\n"
            + "WWWW's Birthday is 12-17-77";
    Matcher matcher = expression.matcher(string1);
    while (matcher.find()) {
      String tmp = matcher.group();
      if (!tmp.equals("WWWW's Birthday is 12-17-77"))
        return false;
    }
    return true;
  }
}
'''

    abstracted = abstract_java_literal_regex_find_for_openjml(source)

    assert "Pattern.compile" not in abstracted
    assert "Matcher matcher" not in abstracted
    assert 'String[] matcher__groups = new String[] {"WWWW\'s Birthday is 12-17-77"};' in abstracted
    assert "String tmp = matcher__group;" in abstracted

    annotated_loop = source.replace(
        "    while (matcher.find()) {",
        "    //@ maintaining true;\n    //@ decreases 0;\n    while (matcher.find()) {",
    )
    annotated_abstracted = abstract_java_literal_regex_find_for_openjml(annotated_loop)
    assert "maintaining true" not in annotated_abstracted
    assert "Matcher matcher" not in annotated_abstracted


def test_literal_regex_find_abstraction_leaves_input_dependent_or_complex_matchers():
    input_dependent = r'''
import java.util.regex.Matcher;
import java.util.regex.Pattern;

class RegexMatches {
  boolean f(String string1) {
    Pattern expression = Pattern.compile("W.*\\d[0-35-9]-\\d\\d-\\d\\d");
    Matcher matcher = expression.matcher(string1);
    while (matcher.find()) {
      String tmp = matcher.group();
      if (!tmp.equals("WWWW's Birthday is 12-17-77"))
        return false;
    }
    return true;
  }
}
'''
    complex_body = r'''
import java.util.regex.Matcher;
import java.util.regex.Pattern;

class RegexMatches {
  boolean f() {
    Pattern expression = Pattern.compile("x+");
    String string1 = "xxx";
    Matcher matcher = expression.matcher(string1);
    while (matcher.find()) {
      if (matcher.start() == 0)
        return true;
    }
    return false;
  }
}
'''

    assert abstract_java_literal_regex_find_for_openjml(input_dependent) == input_dependent
    assert abstract_java_literal_regex_find_for_openjml(complex_body) == complex_body


def test_char_array_slice_equals_abstraction_folds_impossible_literal_length():
    source = """
class StringValueOf {
  boolean f(String arg) {
    if (arg.length() < 8)
      return false;
    char[] charArray = {
      arg.charAt(0), arg.charAt(1), arg.charAt(2),
      arg.charAt(3), arg.charAt(4), arg.charAt(5),
      arg.charAt(6), arg.charAt(7)
    };
    String tmp = String.valueOf(charArray, 3, 3);
    return tmp.equals("fbbl");
  }
}
"""

    abstracted = abstract_java_char_array_slice_equals_for_openjml(source)

    assert "String.valueOf" not in abstracted
    assert 'String tmp = "";' in abstracted
    assert "return false;" in abstracted


def test_char_array_slice_equals_abstraction_requires_static_bounds_and_all_uses_folded():
    equal_length = """
class StringValueOf {
  boolean f(String arg) {
    char[] chars = {arg.charAt(0), arg.charAt(1), arg.charAt(2)};
    String tmp = String.valueOf(chars, 0, 3);
    return tmp.equals("abc");
  }
}
"""
    dynamic_count = """
class StringValueOf {
  boolean f(char[] chars, int n) {
    String tmp = String.valueOf(chars, 0, n);
    return tmp.equals("abc");
  }
}
"""
    remaining_use = """
class StringValueOf {
  boolean f(String arg) {
    char[] chars = {arg.charAt(0), arg.charAt(1), arg.charAt(2)};
    String tmp = String.valueOf(chars, 0, 3);
    return tmp.equals("abcd") || tmp.length() == 3;
  }
}
"""

    assert abstract_java_char_array_slice_equals_for_openjml(equal_length) == equal_length
    assert abstract_java_char_array_slice_equals_for_openjml(dynamic_count) == dynamic_count
    assert abstract_java_char_array_slice_equals_for_openjml(remaining_use) == remaining_use


def test_tochararray_first_char_concat_abstraction_folds_exact_dataflow():
    source = """
class charArray {
  public static char[] f(char c[]) {
    if (c != null && c.length > 0) {
      c[0] = 's';
    }
    return c;
  }

  public static int fun(String arg) {
    if (arg.length() != 5) return -1;
    char[] c = f(arg.toCharArray());
    String s = new String("HELLO") + new String(c, 0, c.length);
    return (s.charAt(5) == 's') ? 1 : 0;
  }
}
"""

    abstracted = abstract_java_tochararray_first_char_concat_for_openjml(source)

    assert "arg.toCharArray()" not in abstracted
    assert 'new String("HELLO")' not in abstracted
    assert "if (arg.length() != 5) return -1;" in abstracted
    assert "return 1;" in abstracted


def test_tochararray_first_char_concat_abstraction_requires_exact_proof_shape():
    mismatched_index = """
class charArray {
  public static char[] f(char c[]) {
    if (c != null && c.length > 0) {
      c[0] = 's';
    }
    return c;
  }
  public static int fun(String arg) {
    if (arg.length() != 5) return -1;
    char[] c = f(arg.toCharArray());
    String s = new String("HELL") + new String(c, 0, c.length);
    return (s.charAt(5) == 's') ? 1 : 0;
  }
}
"""
    mismatched_char = """
class charArray {
  public static char[] f(char c[]) {
    if (c != null && c.length > 0) {
      c[0] = 'x';
    }
    return c;
  }
  public static int fun(String arg) {
    if (arg.length() != 5) return -1;
    char[] c = f(arg.toCharArray());
    String s = new String("HELLO") + new String(c, 0, c.length);
    return (s.charAt(5) == 's') ? 1 : 0;
  }
}
"""
    non_exact_helper = """
class charArray {
  public static char[] f(char c[]) {
    if (c != null && c.length > 1) {
      c[1] = 's';
    }
    return c;
  }
  public static int fun(String arg) {
    if (arg.length() != 5) return -1;
    char[] c = f(arg.toCharArray());
    String s = new String("HELLO") + new String(c, 0, c.length);
    return (s.charAt(5) == 's') ? 1 : 0;
  }
}
"""

    assert abstract_java_tochararray_first_char_concat_for_openjml(mismatched_index) == mismatched_index
    assert abstract_java_tochararray_first_char_concat_for_openjml(mismatched_char) == mismatched_char
    assert abstract_java_tochararray_first_char_concat_for_openjml(non_exact_helper) == non_exact_helper


def test_dropped_wrapper_conversion_abstraction_removes_unused_literal_conversion():
    source = """
class TestLazy {
  boolean f(int a) {
    Integer i = null;
    if (a < 5) {
      i = Integer.valueOf(4);
      i.floatValue();
    } else {
      return false;
    }
    return true;
  }
}
"""

    abstracted = abstract_java_dropped_wrapper_conversion_for_openjml(source)

    assert "Integer.valueOf" not in abstracted
    assert "floatValue" not in abstracted
    assert "Integer i = null;" in abstracted


def test_dropped_wrapper_conversion_abstraction_requires_no_later_use_and_literal_value():
    later_use = """
class TestLazy {
  boolean f(int a) {
    Integer i = null;
    i = Integer.valueOf(4);
    i.floatValue();
    return i.intValue() == 4;
  }
}
"""
    nonliteral_value = """
class TestLazy {
  boolean f(int a) {
    Integer i = null;
    i = Integer.valueOf(a);
    i.floatValue();
    return true;
  }
}
"""

    assert abstract_java_dropped_wrapper_conversion_for_openjml(later_use) == later_use
    assert abstract_java_dropped_wrapper_conversion_for_openjml(nonliteral_value) == nonliteral_value


def test_string_valueof_object_self_concat_abstraction_folds_impossible_compare():
    source = """
class StringValueOf {
  boolean f(String arg) {
    Object objectRef = arg; // assign string to an Object reference
    String tmp = String.valueOf(objectRef);
    return tmp.equals(arg + "s");
  }
}
"""

    abstracted = abstract_java_string_valueof_object_self_concat_for_openjml(source)

    assert "String.valueOf" not in abstracted
    assert "Object objectRef" not in abstracted
    assert "return false;" in abstracted


def test_string_valueof_object_self_concat_abstraction_requires_string_and_nonempty_concat():
    empty_suffix = """
class StringValueOf {
  boolean f(String arg) {
    Object objectRef = arg;
    String tmp = String.valueOf(objectRef);
    return tmp.equals(arg + "");
  }
}
"""
    non_string_source = """
class StringValueOf {
  boolean f(Object arg) {
    Object objectRef = arg;
    String tmp = String.valueOf(objectRef);
    return tmp.equals(arg + "s");
  }
}
"""
    unrelated_concat = """
class StringValueOf {
  boolean f(String arg, String other) {
    Object objectRef = arg;
    String tmp = String.valueOf(objectRef);
    return tmp.equals(other + "s");
  }
}
"""

    assert abstract_java_string_valueof_object_self_concat_for_openjml(empty_suffix) == empty_suffix
    assert abstract_java_string_valueof_object_self_concat_for_openjml(non_string_source) == non_string_source
    assert abstract_java_string_valueof_object_self_concat_for_openjml(unrelated_concat) == unrelated_concat


def test_impossible_string_affix_equals_abstraction_folds_false_comparisons():
    source = """
class OverapproximationString {
  void f(String s) {
    String prefix = "abc";
    String complete = prefix + s;
    if (complete.equals("not possible")) {
      assert true;
    } else {
      assert false;
    }
    String suffixed = s + ".java";
    if ("README.md".equals(suffixed)) {
      assert false;
    }
  }
}
"""

    abstracted = abstract_java_impossible_string_affix_equals_for_openjml(source)

    assert 'complete.equals("not possible")' not in abstracted
    assert '"README.md".equals(suffixed)' not in abstracted
    assert 'String complete = "";' in abstracted
    assert 'String suffixed = "";' in abstracted
    assert abstracted.count("if (false)") == 2


def test_impossible_string_affix_equals_abstraction_keeps_possible_or_unknown_comparisons():
    possible_prefix = """
class OverapproximationString {
  void f(String s) {
    String prefix = "abc";
    String complete = prefix + s;
    if (complete.equals("abcdef")) assert true;
  }
}
"""
    possible_suffix = """
class OverapproximationString {
  void f(String s) {
    String complete = s + ".java";
    if (complete.equals("Main.java")) assert true;
  }
}
"""
    reassigned = """
class OverapproximationString {
  void f(String s) {
    String prefix = "abc";
    String complete = prefix + s;
    complete = s;
    if (complete.equals("not possible")) assert true;
  }
}
"""

    assert abstract_java_impossible_string_affix_equals_for_openjml(possible_prefix) == possible_prefix
    assert abstract_java_impossible_string_affix_equals_for_openjml(possible_suffix) == possible_suffix
    assert abstract_java_impossible_string_affix_equals_for_openjml(reassigned) == reassigned


def test_literal_string_comparison_loops_fold_reverse_and_getchars_prefix_checks():
    source = """
class StringMiscellaneous {
  boolean f() {
    String s1 = "Automatic Test Generation";
    String s2 = "noitareneG tseT citamotuA";
    String s3 = "Autom";
    char[] charArray = new char[5];

    int i = 0;
    for (int count = s1.length() - 1; count >= 0; count--) {
      System.out.printf("%c ", s1.charAt(count));
      if(s1.charAt(count) != s2.charAt(i)) return false;
      ++i;
    }

    s1.getChars(0, 5, charArray, 0);
    i = 0;
    for (char character : charArray) {
      System.out.print(character);
      if(s3.charAt(i) != character) return false;
      ++i;
    }
    return true;
  }
}
"""

    abstracted = abstract_java_literal_string_comparison_loops_for_openjml(
        abstract_java_debug_output_for_openjml(source)
    )

    assert "for (int count" not in abstracted
    assert "getChars" not in abstracted
    assert "for (char character" not in abstracted
    assert "int i = 25;" in abstracted
    assert "i = 5;" in abstracted


def test_literal_string_comparison_loops_require_true_literal_relations_and_dead_array():
    not_reverse = """
class StringMiscellaneous {
  boolean f() {
    String s1 = "abc";
    String s2 = "abc";
    int i = 0;
    for (int count = s1.length() - 1; count >= 0; count--) {
      if(s1.charAt(count) != s2.charAt(i)) return false;
      ++i;
    }
    return true;
  }
}
"""
    live_array = """
class StringMiscellaneous {
  char[] f() {
    String s1 = "abc";
    String s2 = "ab";
    char[] charArray = new char[2];
    int i = 0;
    s1.getChars(0, 2, charArray, 0);
    i = 0;
    for (char character : charArray) {
      if(s2.charAt(i) != character) return null;
      ++i;
    }
    return charArray;
  }
}
"""

    assert abstract_java_literal_string_comparison_loops_for_openjml(not_reverse) == not_reverse
    assert abstract_java_literal_string_comparison_loops_for_openjml(live_array) == live_array


def test_literal_string_array_foreach_abstraction_folds_simple_counts():
    source = """
class StringStartEnd {
  void f() {
    String[] strings = {"tested", "testing", "passed", "passing"};
    int i = 0;
    for (String string : strings) {
      if (string.startsWith("te")) ++i;
    }
    assert i == 2;
    i = 0;
    for (String string : strings) {
      if (string.startsWith("ste", 2)) i++;
    }
    assert i == 1;
    i = 0;
    for (String string : strings) {
      if (string.endsWith("ed")) i += 1;
    }
    assert i == 2;
  }
}
"""

    abstracted = abstract_java_literal_string_array_foreach_for_openjml(source)

    assert 'for (String string : strings)' not in abstracted
    assert abstracted.count("i += 2;") == 2
    assert "i += 1;" in abstracted


def test_literal_string_array_foreach_abstraction_leaves_input_dependent_arrays():
    source = """
class StringStartEnd {
  void f(String a, String b) {
    String[] strings = new String[2];
    strings[0] = a;
    strings[1] = b;
    int i = 0;
    for (String string : strings) {
      if (string.endsWith("ed")) ++i;
    }
  }
}
"""

    assert abstract_java_literal_string_array_foreach_for_openjml(source) == source


def test_constant_null_try_catch_abstraction_folds_deterministic_npe_branch():
    source = """
class A { int i; }
class NullPointerExceptionExample {
  boolean f() {
    A a = null;
    try {
      a.i = 0;
    } catch (NullPointerException exc) {
      return false;
    }
    return true;
  }
  boolean g() {
    A a = null;
    try {
      int i = a.i;
    } catch (Exception exc) {
      return false;
    }
    return true;
  }
}
"""

    abstracted = abstract_java_constant_null_try_catch_for_openjml(source)

    assert "try" not in abstracted
    assert abstracted.count("return false;") == 2
    assert "return true;" not in abstracted


def test_constant_null_try_catch_abstraction_folds_empty_catch_fallthrough():
    source = """
class NullPointerExceptionExample {
  boolean f() {
    Object o = null;
    try {
      o.hashCode();
      // unreachable after the deterministic NPE
      return false;
    } catch (Exception e) {
    }
    return true;
  }
}
"""

    abstracted = abstract_java_constant_null_try_catch_for_openjml(source)

    assert "try" not in abstracted
    assert "catch" not in abstracted
    assert "return false;" not in abstracted
    assert "return true;" in abstracted


def test_constant_null_try_catch_abstraction_leaves_nontrivial_or_nonnull_try_blocks():
    nontrivial = """
class A { int i; }
class NullPointerExceptionExample {
  boolean f(A a) {
    try {
      a.i = 0;
    } catch (NullPointerException exc) {
      return false;
    }
    return true;
  }
}
"""
    multistatement = """
class A { int i; }
class NullPointerExceptionExample {
  boolean f() {
    A a = null;
    try {
      int x = 0;
      a.i = x;
    } catch (NullPointerException exc) {
      return false;
    }
    return true;
  }
}
"""

    assert abstract_java_constant_null_try_catch_for_openjml(nontrivial) == nontrivial
    assert abstract_java_constant_null_try_catch_for_openjml(multistatement) == multistatement


def test_simple_stringbuilder_abstraction_tracks_literal_capacity_and_setlength():
    source = """
class StringBuilderCapLen {
  void f() {
    StringBuilder buffer =
        new StringBuilder("Diffblue is leader in automatic test case generation");
    assert buffer.toString().equals("Diffblue is leader in automatic test case generation");
    assert buffer.length() == 52;
    assert buffer.capacity() == 68;
    buffer.ensureCapacity(75);
    assert buffer.capacity() == 138;
    buffer.setLength(8);
    assert buffer.length() == 8;
    assert buffer.toString().equals("Diffblue");
  }
}
"""

    abstracted = abstract_java_simple_stringbuilder_for_openjml(source)

    assert "StringBuilder buffer" not in abstracted
    assert "new StringBuilder" not in abstracted
    assert 'String buffer = "Diffblue is leader in automatic test case generation";' in abstracted
    assert 'buffer = "Diffblue";' in abstracted
    assert "assert 52 == 52;" in abstracted
    assert "assert 68 == 68;" in abstracted
    assert "assert 138 == 138;" in abstracted
    assert 'assert "Diffblue".equals("Diffblue");' in abstracted


def test_simple_stringbuilder_abstraction_folds_literal_append_chain():
    source = """
class StringBuilderAppend {
  void f() {
    Object objectRef = "diffblue";
    String string = "test";
    char[] charArray = {'v', 'e', 'r', 'i', 'f', 'i'};
    boolean booleanValue = true;
    char characterValue = 'Z';
    int integerValue = 7;
    long longValue = 10000000000L;
    StringBuilder lastBuffer = new StringBuilder("last buffer");
    StringBuilder buffer = new StringBuilder();
    buffer
        .append(objectRef)
        .append("%n")
        .append(string)
        .append("%n")
        .append(charArray)
        .append("%n")
        .append(charArray, 0, 3)
        .append("%n")
        .append(booleanValue)
        .append("%n")
        .append(characterValue)
        .append("%n")
        .append(integerValue)
        .append("%n")
        .append(longValue)
        .append("%n")
        .append(lastBuffer);
    String tmp = buffer.toString();
  }
}
"""

    abstracted = abstract_java_simple_stringbuilder_for_openjml(source)

    assert "StringBuilder buffer" not in abstracted
    assert "new StringBuilder" not in abstracted
    assert 'buffer = "diffblue%ntest%nverifi%nver%ntrue%nZ%n7%n10000000000%nlast buffer";' in abstracted
    assert 'String tmp = "diffblue%ntest%nverifi%nver%ntrue%nZ%n7%n10000000000%nlast buffer";' in abstracted


def test_simple_stringbuilder_abstraction_rewrites_input_dependent_length():
    source = """
class StringBuilderLen {
  boolean f(String arg) {
    StringBuilder buffer = new StringBuilder(arg);
    return buffer.length() == 51;
  }
}
"""

    abstracted = abstract_java_simple_stringbuilder_for_openjml(source)

    assert "StringBuilder buffer" not in abstracted
    assert "new StringBuilder" not in abstracted
    assert "String buffer = arg;" in abstracted
    assert "return arg.length() == 51;" in abstracted


def test_simple_stringbuilder_abstraction_bails_on_unhandled_mutation():
    source = """
class StringBuilderChars {
  void f(String arg) {
    StringBuilder buffer = new StringBuilder(arg);
    char[] chars = new char[buffer.length()];
    buffer.getChars(0, buffer.length(), chars, 0);
  }
}
"""

    assert abstract_java_simple_stringbuilder_for_openjml(source) == source


def test_stringbuilder_getchars_self_compare_abstraction_folds_exact_full_copy_loop():
    source = """
class StringBuilderChars {
  boolean f(String arg) {
    StringBuilder buffer = new StringBuilder(arg);

    char[] charArray = new char[buffer.length()];
    buffer.getChars(0, buffer.length(), charArray, 0);

    int i = 0;
    //@ maintaining 0 <= i && i <= charArray.length;
    //@ decreases charArray.length - i;
    for (char character : charArray) {
      System.out.print(character);
      if (character == buffer.charAt(i))
        return false;
      ++i;
    }
    return true;
  }
}
"""

    abstracted = abstract_java_stringbuilder_getchars_self_compare_for_openjml(source)

    assert "StringBuilder buffer" not in abstracted
    assert "getChars" not in abstracted
    assert "return arg.length() == 0;" in abstracted


def test_stringbuilder_getchars_self_compare_abstraction_requires_exact_shape():
    opposite_check = """
class StringBuilderChars {
  boolean f(String arg) {
    StringBuilder buffer = new StringBuilder(arg);
    char[] charArray = new char[buffer.length()];
    buffer.getChars(0, buffer.length(), charArray, 0);
    int i = 0;
    for (char character : charArray) {
      if (!(character == buffer.charAt(i))) return false;
      ++i;
    }
    return true;
  }
}
"""
    extra_operations = """
class StringBuilderChars {
  boolean f() {
    StringBuilder buffer = new StringBuilder("DiffBlue Limited");
    if (!buffer.toString().equals("DiffBlue Limited")) return false;
    char[] charArray = new char[buffer.length()];
    buffer.getChars(0, buffer.length(), charArray, 0);
    int i = 0;
    for (char character : charArray) {
      if (character == buffer.charAt(i)) return false;
      ++i;
    }
    return true;
  }
}
"""

    assert abstract_java_stringbuilder_getchars_self_compare_for_openjml(opposite_check) == opposite_check
    assert abstract_java_stringbuilder_getchars_self_compare_for_openjml(extra_operations) == extra_operations


def test_source_code_preserved_allows_only_constant_split_abstraction():
    original = """
class TokenTest {
  boolean f() {
    String sentence = "a b";
    String[] tokens = sentence.split(" ");
    return tokens.length == 2;
  }
}
"""
    split_abstracted = """
class TokenTest {
  boolean f() {
    String sentence = "a b";
    String[] tokens = new String[] {"a", "b"};
    return tokens.length == 2;
  }
}
"""
    changed = """
class TokenTest {
  boolean f() {
    String sentence = "a b";
    String[] tokens = new String[] {"a"};
    return tokens.length == 2;
  }
}
"""

    ok, err = source_code_preserved(original, split_abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_constant_string_construction_abstraction():
    original = """
class StringConstruction {
  void f() {
    char[] chars = {'d', 'i', 'f', 'f'};
    String s = String.valueOf(chars, 1, 2);
  }
}
"""
    construction_abstracted = """
class StringConstruction {
  void f() {
    char[] chars = {'d', 'i', 'f', 'f'};
    String s = "if";
  }
}
"""
    changed = """
class StringConstruction {
  void f() {
    char[] chars = {'d', 'i', 'f', 'f'};
    String s = "di";
  }
}
"""

    ok, err = source_code_preserved(original, construction_abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_rejects_identity_sensitive_new_string_fold():
    original = """
class StringCompare {
  void f() {
    String s1 = new String("test");
    if (s1 != "test") assert true;
  }
}
"""
    changed = """
class StringCompare {
  void f() {
    String s1 = "test";
    if (s1 != "test") assert true;
  }
}
"""

    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_constant_string_method_folding():
    original = """
class StringMethods {
  void f() {
    String s = "tested";
    assert s.startsWith("te");
    assert s.charAt(1) == 'e';
  }
}
"""
    abstracted = """
class StringMethods {
  void f() {
    String s = "tested";
    assert true;
    assert 'e' == 'e';
  }
}
"""
    changed = abstracted.replace("assert true;", "assert false;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_literal_string_array_foreach_counts():
    original = """
class StringStartEnd {
  void f() {
    String[] strings = {"tested", "testing", "passed", "passing"};
    int i = 0;
    for (String string : strings) {
      if (string.startsWith("te")) ++i;
    }
    assert i == 2;
  }
}
"""
    abstracted = """
class StringStartEnd {
  void f() {
    String[] strings = {"tested", "testing", "passed", "passing"};
    int i = 0;
    i += 2;
    assert i == 2;
  }
}
"""
    changed = abstracted.replace("i += 2;", "i += 3;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_constant_null_try_catch_folding():
    original = """
class A { int i; }
class NullPointerExceptionExample {
  boolean f() {
    A a = null;
    try {
      a.i = 0;
    } catch (NullPointerException exc) {
      return false;
    }
    return true;
  }
}
"""
    abstracted = """
class A { int i; }
class NullPointerExceptionExample {
  boolean f() {
    A a = null;
    return false;
  }
}
"""
    changed = abstracted.replace("return false;", "return true;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_constant_null_empty_catch_folding():
    original = """
class NullPointerExceptionExample {
  boolean f() {
    Object o = null;
    try {
      o.hashCode();
      return false;
    } catch (Exception e) {
    }
    return true;
  }
}
"""
    abstracted = """
class NullPointerExceptionExample {
  boolean f() {
    Object o = null;
    return true;
  }
}
"""
    changed = abstracted.replace("return true;", "return false;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_charsequence_string_alias_folding():
    original = """
class CharSequenceToString {
  boolean f(String arg) {
    CharSequence cs = (CharSequence) arg;
    String s = cs.toString();
    int i = -1;
    if (s.equals("case1")) i = cs.length();
    return i == -1 || i == 5;
  }
}
"""
    abstracted = """
class CharSequenceToString {
  boolean f(String arg) {
    String s = arg;
    int i = -1;
    if (s.equals("case1")) i = s.length();
    return i == -1 || i == 5;
  }
}
"""
    changed = abstracted.replace("i == 5", "i == 6")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_literal_regex_find_folding():
    original = r'''
import java.util.regex.Matcher;
import java.util.regex.Pattern;

class RegexMatches {
  boolean f() {
    Pattern expression = Pattern.compile("x+");
    String string1 = "xxx";
    Matcher matcher = expression.matcher(string1);
    while (matcher.find()) {
      String tmp = matcher.group();
      if (!tmp.equals("xxx"))
        return false;
    }
    return true;
  }
}
'''
    abstracted = r'''
import java.util.regex.Matcher;
import java.util.regex.Pattern;

class RegexMatches {
  boolean f() {
    String string1 = "xxx";
    String[] matcher__groups = new String[] {"xxx"};
    for (String matcher__group : matcher__groups) {
      String tmp = matcher__group;
      if (!tmp.equals("xxx"))
        return false;
    }
    return true;
  }
}
'''
    changed = abstracted.replace('tmp.equals("xxx")', 'tmp.equals("xx")')

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_char_array_slice_equals_folding():
    original = """
class StringValueOf {
  boolean f(String arg) {
    if (arg.length() < 8)
      return false;
    char[] charArray = {
      arg.charAt(0), arg.charAt(1), arg.charAt(2),
      arg.charAt(3), arg.charAt(4), arg.charAt(5),
      arg.charAt(6), arg.charAt(7)
    };
    String tmp = String.valueOf(charArray, 3, 3);
    return tmp.equals("fbbl");
  }
}
"""
    abstracted = """
class StringValueOf {
  boolean f(String arg) {
    if (arg.length() < 8)
      return false;
    char[] charArray = {
      arg.charAt(0), arg.charAt(1), arg.charAt(2),
      arg.charAt(3), arg.charAt(4), arg.charAt(5),
      arg.charAt(6), arg.charAt(7)
    };
    String tmp = "";
    return false;
  }
}
"""
    changed = abstracted.replace("return false;", "return true;", 1)

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_tochararray_first_char_concat():
    original = """
class charArray {
  public static char[] f(char c[]) {
    if (c != null && c.length > 0) {
      c[0] = 's';
    }
    return c;
  }

  public static int fun(String arg) {
    if (arg.length() != 5) return -1;
    char[] c = f(arg.toCharArray());
    String s = new String("HELLO") + new String(c, 0, c.length);
    return (s.charAt(5) == 's') ? 1 : 0;
  }
}
"""
    abstracted = """
class charArray {
  public static char[] f(char c[]) {
    if (c != null && c.length > 0) {
      c[0] = 's';
    }
    return c;
  }

  public static int fun(String arg) {
    if (arg.length() != 5) return -1;
    return 1;
  }
}
"""
    changed = abstracted.replace("return 1;", "return 0;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_dropped_wrapper_conversion():
    original = """
class TestLazy {
  boolean f(int a) {
    Integer i = null;
    if (a < 5) {
      i = Integer.valueOf(4);
      i.floatValue();
    } else {
      return false;
    }
    return true;
  }
}
"""
    abstracted = """
class TestLazy {
  boolean f(int a) {
    Integer i = null;
    if (a < 5) {
      ;
    } else {
      return false;
    }
    return true;
  }
}
"""
    changed = abstracted.replace("return true;", "return false;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_simple_stringbuilder_abstraction():
    original = """
class StringBuilderCapLen {
  void f() {
    StringBuilder buffer = new StringBuilder("abcdef");
    assert buffer.length() == 6;
    buffer.setLength(3);
    assert buffer.toString().equals("abc");
  }
}
"""
    abstracted = """
class StringBuilderCapLen {
  void f() {
    String buffer = "abcdef";
    assert 6 == 6;
    buffer = "abc";
    assert "abc".equals("abc");
  }
}
"""
    changed = abstracted.replace('"abc"', '"abd"', 1)

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_string_valueof_object_self_concat():
    original = """
class StringValueOf {
  boolean f(String arg) {
    Object objectRef = arg;
    String tmp = String.valueOf(objectRef);
    return tmp.equals(arg + "s");
  }
}
"""
    abstracted = """
class StringValueOf {
  boolean f(String arg) {
    return false;
  }
}
"""
    changed = abstracted.replace("return false;", "return true;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_impossible_string_affix_equals():
    original = """
class OverapproximationString {
  void f(String s) {
    String prefix = "abc";
    String complete = prefix + s;
    if (complete.equals("not possible")) {
      assert true;
    } else {
      assert false;
    }
  }
}
"""
    abstracted = """
class OverapproximationString {
  void f(String s) {
    String prefix = "abc";
    String complete = "";
    if (false) {
      assert true;
    } else {
      assert false;
    }
  }
}
"""
    changed = abstracted.replace("if (false)", "if (true)")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_literal_string_comparison_loops():
    original = """
class StringMiscellaneous {
  boolean f() {
    String s1 = "abc";
    String s2 = "cba";
    int i = 0;
    for (int count = s1.length() - 1; count >= 0; count--) {
      if(s1.charAt(count) != s2.charAt(i)) return false;
      ++i;
    }
    return true;
  }
}
"""
    abstracted = """
class StringMiscellaneous {
  boolean f() {
    String s1 = "abc";
    String s2 = "cba";
    int i = 3;
    return true;
  }
}
"""
    changed = abstracted.replace("return true;", "return false;")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


def test_source_code_preserved_allows_only_stringbuilder_getchars_self_compare():
    original = """
class StringBuilderChars {
  boolean f(String arg) {
    StringBuilder buffer = new StringBuilder(arg);
    char[] charArray = new char[buffer.length()];
    buffer.getChars(0, buffer.length(), charArray, 0);
    int i = 0;
    for (char character : charArray) {
      if (character == buffer.charAt(i))
        return false;
      ++i;
    }
    return true;
  }
}
"""
    abstracted = """
class StringBuilderChars {
  boolean f(String arg) {
    return arg.length() == 0;
  }
}
"""
    changed = abstracted.replace("== 0", "!= 0")

    ok, err = source_code_preserved(original, abstracted)
    assert ok, err
    ok, err = source_code_preserved(original, changed)
    assert not ok
    assert "executable Java code" in err


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
    public int f(int n) {
        int s = 0;
        //@ maintaining s == (\\sum int k; k in 0..i; k);
        //@ decreases n - i;
        for (int i = 0; i < n; i++) {
            s += i;
        }
        return s;
    }
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


def test_normalize_rewrites_simple_conditional_ensures():
    original = """
public class X {
    //@ ensures \\result == a + b && op == '+';
    int f(int a, int b, char op) {
        return a + b;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ ensures op == '+' ==> \\result == a + b;" in normalized


def test_normalize_drops_method_contract_referencing_locals():
    original = """
class Area {
    //@ requires ax1 <= ax2;
    //@ ensures \\result == area1 + overlapArea;
    int area(int ax1, int ax2) {
        int area1 = ax2 - ax1;
        int overlapArea = 0;
        return area1 + overlapArea;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "requires ax1 <= ax2" in normalized
    assert "//@ ensures \\result == area1 + overlapArea;" not in normalized
    assert "return area1 + overlapArea;" in normalized


def test_normalize_drops_malformed_unbalanced_jml_clause():
    original = """
class PowerOfTwo {
    //@ ensures (n & (n - 1)) == 0) ==> \\result == (n > 0;
    boolean isPowerOfTwo(int n) {
        return n > 0 && (n & (n - 1)) == 0;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "ensures" not in normalized
    assert "return n > 0" in normalized


def test_normalize_preserves_balanced_multiline_jml_clause():
    original = """
class FindClosestNum {
    //@ ensures (\\forall int i; 0 <= i && i < nums.length;
    //@     ((\\result < 0 ? -\\result : \\result) <= (nums[i] < 0 ? -nums[i] : nums[i])));
    int findClosestNumber(int[] nums) {
        return nums[0];
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "ensures (\\forall int i;" in normalized
    assert "nums[i]" in normalized


def test_normalize_drops_loop_specs_not_adjacent_to_loop():
    original = """
class ConvertToTitle {
    void f(int n) {
        //@ maintaining n >= 0;
        //@ decreases n;
        while (n > 0) {
            n--;
            //@ maintaining n >= 0;
            //@ decreases n;
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert normalized.count("maintaining n >= 0") == 1
    assert normalized.count("decreases n") == 1
    assert "while (n > 0)" in normalized


def test_normalize_keeps_loop_specs_before_nested_loop():
    original = """
class Nested {
    void f(int n) {
        //@ maintaining 0 <= i && i <= n;
        for (int i = 0; i < n; i++) {
            //@ maintaining 0 <= j && j <= n;
            //@ decreases n - j;
            for (int j = 0; j < n; j++) {
            }
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "maintaining 0 <= i && i <= n" in normalized
    assert "maintaining 0 <= j && j <= n" in normalized
    assert "decreases n - j" in normalized


def test_normalize_renames_quantifier_that_shadows_for_loop_variable():
    original = """
class RepeatedNumNested {
    int f(int[] arr) {
        for (int i = 0; i < arr.length; i++) {
            //@ maintaining 0 <= j && (\\forall int j; i <= j && j < arr.length; arr[i] != arr[j]);
            //@ decreases arr.length - j;
            for (int j = i + 1; j < arr.length; j++) {
            }
        }
        return -1;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "\\forall int j_q;" in normalized
    assert "i <= j_q && j_q < arr.length" in normalized
    assert "arr[j_q]" in normalized
    assert "0 <= j &&" in normalized
    assert "decreases arr.length - j" in normalized


def test_normalize_renames_method_contract_quantifiers_that_shadow_locals():
    original = """
class MatrixAdd {
    //@ requires (\\forall int i; 0 <= i && i < 2; a[i] != null);
    //@ ensures (\\forall int i; 0 <= i && i < 2; (\\forall int j; 0 <= j && j < 2; \\result[i][j] == a[i][j]));
    int[][] add(int[][] a) {
        int[][] c = new int[2][2];
        for (int i = 0; i < 2; i++) {
            for (int j = 0; j < 2; j++) {
                c[i][j] = a[i][j];
            }
        }
        return c;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "\\forall int i_q;" in normalized
    assert "\\forall int j_q;" in normalized
    assert "a[i_q] != null" in normalized
    assert "\\result[i_q][j_q] == a[i_q][j_q]" in normalized
    assert "for (int i = 0; i < 2; i++)" in normalized
    assert "for (int j = 0; j < 2; j++)" in normalized


def test_normalize_strips_inline_spec_public_modifier():
    original = """
public class PrimeNumbers {
    private int /*@ spec_public @*/ primeArray[];
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "spec_public" not in normalized
    assert "private int primeArray[]" in normalized


def test_prune_reported_annotation_error_removes_jml_line():
    source = """
class LargestPerimeter {
    int f(int[] a) {
        //@ maintaining \\result == 0;
        for (int i = 0; i < a.length; i++) {
        }
        return 0;
    }
}
"""
    output = "X.java:4: error: A \\result expression may not be in a loop_invariant clause"
    pruned, changed = _prune_reported_annotation_error(source, output)
    assert changed
    assert "\\result == 0" not in pruned
    assert "for (int i" in pruned


def test_prune_reported_annotation_error_removes_all_reported_jml_lines():
    source = """
class X {
    int a;
    int b;
    //@ assignable a;
    public void f() {}
    //@ assignable b;
    public void g() {}
}
"""
    output = "\n".join(
        [
            "X.java:5: error: An identifier with private visibility may not be used in a assignable clause with public visibility",
            "X.java:7: error: An identifier with private visibility may not be used in a assignable clause with public visibility",
        ]
    )
    pruned, changed = _prune_reported_annotation_error(source, output)
    assert changed
    assert "assignable a" not in pruned
    assert "assignable b" not in pruned
    assert "public void f()" in pruned
    assert "public void g()" in pruned


def test_has_reported_jml_annotation_error_distinguishes_jml_from_java_errors():
    source = """
class LinkedList {
  LinkedListEntry Head;
  //@ assignable Head, Next, Value;
  void add() {
    MissingSymbol x;
  }
}
"""
    jml_output = "X.java:4: error: cannot find symbol\n  //@ assignable Head, Next, Value;\n"
    java_output = "X.java:6: error: cannot find symbol\n    MissingSymbol x;\n"

    assert _has_reported_jml_annotation_error(source, jml_output)
    assert not _has_reported_jml_annotation_error(source, java_output)


def test_prune_reported_annotation_error_removes_block_line():
    source = """
class IsAllUnique {
    /*@
      requires s != null;
      ensures !\\result ==> (\\exists int i; 0 <= i && i < s.length();
    @*/
    boolean f(String s) { return true; }
}
"""
    output = "X.java:5: error: Incorrectly formed or terminated ensures statement near here"
    pruned, changed = _prune_reported_annotation_error(source, output)
    assert changed
    assert "ensures !\\result" not in pruned
    assert "requires s != null" in pruned


def test_prune_enclosing_loop_specs_for_internal_error_removes_nearest_loop_group():
    source = """
class MatrixAdd {
    //@ ensures \\result != null;
    int[][] add(int[][] a) {
        int[][] c = new int[2][2];
        //@ maintaining 0 <= i && i <= 2;
        //@ maintaining c != null;
        //@ decreases 2 - i;
        for (int i = 0; i < 2; i++) {
            //@ maintaining 0 <= j && j <= 2;
            //@ decreases 2 - j;
            for (int j = 0; j < 2; j++) {
                c[i][j] = a[i][j];
            }
        }
        return c;
    }
}
"""
    output = "MatrixAdd.java:12: error: A catastrophic JML internal error occurred.\nReason: Double rewriting of ident: i i_1 i_2\n"
    pruned, changed = _prune_enclosing_loop_specs_for_internal_error(source, output)
    assert changed
    assert "maintaining 0 <= j" not in pruned
    assert "decreases 2 - j" not in pruned
    assert "maintaining 0 <= i" in pruned
    assert "ensures \\result != null" in pruned


def test_prune_enclosing_loop_specs_for_internal_error_handles_unbraced_loop_line():
    source = """
class X {
    void fill(int[][] a, int x, int y) {
        //@ maintaining 0 <= i && i <= x;
        //@ decreases x - i;
        for (int i = 0; i < x; ++i)
            for (int j = 0; j < y; ++j) a[i][j] = i + j;
    }
}
"""
    output = (
        "X.java:7: error: A catastrophic JML internal error occurred.\n"
        "            for (int j = 0; j < y; ++j) a[i][j] = i + j;\n"
        "  Reason: Double rewriting of ident: i i_1 i_2\n"
    )

    pruned, changed = _prune_enclosing_loop_specs_for_internal_error(source, output)

    assert changed
    assert "maintaining 0 <= i" not in pruned
    assert "decreases x - i" not in pruned
    assert "for (int i" in pruned
    assert "for (int j" in pruned


def test_run_openjml_classifies_catastrophic_internal_error_as_tool_error(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "X.java:11: error: A catastrophic JML internal error occurred.  Please report the bug with as much information as you can.\n"
        "  Reason: Double rewriting of ident: i i_1 i_2\n"
        "1 error\n"
        "EOF\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "tool_error"
    assert not result.passed


def test_run_openjml_classifies_recoverable_internal_error_as_tool_error(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "error: An internal JML error occurred, possibly recoverable.  Please report the bug with as much information as you can.\n"
        "  Reason: class com.sun.tools.javac.tree.JCTree$JCAssignOp cannot be cast to class com.sun.tools.javac.tree.JCTree$JCBinary\n"
        "  java.lang.ClassCastException: class com.sun.tools.javac.tree.JCTree$JCAssignOp cannot be cast\n"
        "1 error\n"
        "EOF\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "tool_error"
    assert not result.passed


def test_run_openjml_classifies_proof_script_error_as_tool_error(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "error: An error while executing a proof script for f: (error \"expecting an arithmetic subterm\")\n"
        "1 error\n"
        "EOF\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "tool_error"
    assert not result.passed


def test_run_openjml_treats_returncode_zero_notes_as_passed(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "Note: X.java uses or overrides a deprecated API that is marked for removal.\n"
        "Note: Recompile with -Xlint:removal for details.\n"
        "EOF\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "passed"
    assert result.passed is True


def test_run_openjml_keeps_returncode_zero_null_precondition_as_failure(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "NULL PRECONDITION FOR X.f(java.lang.String) java.util.Scanner.next() false public behavior\n"
        "  assignable \\\\everything;\n"
        "EOF\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "verification_failed"
    assert result.passed is False


def test_run_openjml_classifies_prover_timeout_output_as_timeout(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "X.java:5: verify: Validity is unknown - time or memory limit reached: : Aborted proof: timeout\n"
        "    public boolean f(int[] arr) {\n"
        "                   ^\n"
        "1 verification failure\n"
        "EOF\n"
        "exit 6\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "timeout"
    assert result.passed is False


def test_run_openjml_classifies_unreachable_statement_as_source_invalid(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "X.java:12: error: unreachable statement\n"
        "    return true;\n"
        "    ^\n"
        "1 error\n"
        "EOF\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "source_invalid"
    assert result.passed is False


def test_run_openjml_classifies_missing_symbol_as_source_invalid(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "X.java:11: error: cannot find symbol\n"
        "    return args instanceof Object[];\n"
        "           ^\n"
        "  symbol:   variable args\n"
        "  location: class Main\n"
        "1 error\n"
        "EOF\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X {}\n", encoding="utf-8")

    result = run_openjml(source, openjml_path=str(fake_openjml), timeout_s=1)

    assert result.status == "source_invalid"
    assert result.passed is False


def test_run_process_group_kills_child_group_on_keyboard_interrupt(monkeypatch):
    class FakeProc:
        pid = 4242
        returncode = None

        def communicate(self, timeout=None):
            raise KeyboardInterrupt

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("bmc_agent.jml_specs.subprocess.Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr("bmc_agent.jml_specs.os.killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(KeyboardInterrupt):
        _run_process_group(["openjml"], cwd=None, timeout_s=1)

    assert killed == [(4242, signal.SIGKILL)]

    kill_active_openjml_process_groups()
    assert killed == [(4242, signal.SIGKILL)]


def test_specs_bench_stops_after_openjml_tool_error(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "X.java:8: error: A catastrophic JML internal error occurred.  Please report the bug with as much information as you can.\n"
        "  Reason: Double rewriting of ident: i i_1 i_2\n"
        "1 error\n"
        "EOF\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X { int f() { return 1; } }\n", encoding="utf-8")
    llm = _FakeLLM("class X { //@ ensures \\result == 1;\n int f() { return 1; } }\n")
    config = SimpleNamespace(llm_model="fake", resolved_provider=lambda: "fake")

    result = run_jml_specs_bench(
        source,
        driver="X",
        config=config,
        llm=llm,
        output_dir=tmp_path / "out",
        openjml_path=str(fake_openjml),
        openjml_timeout=1,
        max_iterations=5,
    )

    assert result.status == "tool_error"
    assert len(result.iterations) == 1
    assert llm.calls == 1


def test_specs_bench_recovers_internal_error_by_pruning_loop_specs(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text(
        "#!/bin/sh\n"
        "src=''\n"
        "for arg in \"$@\"; do case \"$arg\" in *.java) src=\"$arg\";; esac; done\n"
        "if grep -q 'maintaining' \"$src\"; then\n"
        "  echo \"$src:8: error: A catastrophic JML internal error occurred.\"\n"
        "  echo 'Reason: Double rewriting of ident: i i_1 i_2'\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_openjml.chmod(0o755)
    source = tmp_path / "X.java"
    source.write_text("class X { int f() { int x = 0; for (int i = 0; i < 1; i++) { x++; } return x; } }\n", encoding="utf-8")
    llm = _FakeLLM(
        """
class X {
  //@ ensures \\result == 1;
  int f() {
    int x = 0;
    //@ maintaining 0 <= i && i <= 1;
    for (int i = 0; i < 1; i++) {
      x++;
    }
    return x;
  }
}
"""
    )
    config = SimpleNamespace(llm_model="fake", resolved_provider=lambda: "fake")

    result = run_jml_specs_bench(
        source,
        driver="X",
        config=config,
        llm=llm,
        output_dir=tmp_path / "out",
        openjml_path=str(fake_openjml),
        openjml_timeout=1,
        max_iterations=5,
    )

    assert result.status == "passed"
    assert result.iterations[0].openjml.status == "passed"
    final_source = Path(result.final_annotated_path).read_text(encoding="utf-8")
    assert "maintaining" not in final_source
    assert "ensures \\result == 1" in final_source


def test_specs_bench_writes_public_class_filename_for_openjml(tmp_path):
    fake_openjml = tmp_path / "openjml"
    fake_openjml.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_openjml.chmod(0o755)
    source = tmp_path / "StringStartEnd03.java"
    source.write_text("public class Main { int f() { return 1; } }\n", encoding="utf-8")
    llm = _FakeLLM("public class Main { //@ ensures \\result == 1;\n int f() { return 1; } }\n")
    config = SimpleNamespace(llm_model="fake", resolved_provider=lambda: "fake")

    result = run_jml_specs_bench(
        source,
        driver="StringStartEnd03",
        config=config,
        llm=llm,
        output_dir=tmp_path / "out",
        openjml_path=str(fake_openjml),
        openjml_timeout=1,
        max_iterations=1,
    )

    assert result.passed is True
    assert Path(result.final_annotated_path).name == "Main.java"
    assert Path(result.iterations[0].annotated_path).name == "Main.java"


def test_specs_bench_passes_iter_support_sources_to_openjml(tmp_path, monkeypatch):
    source = tmp_path / "X.java"
    source.write_text("class X { int f() { return Verifier.nondetInt(); } }\n", encoding="utf-8")
    llm = _FakeLLM("class X { //@ ensures \\result >= 0;\n int f() { return Verifier.nondetInt(); } }\n")
    config = SimpleNamespace(llm_model="fake", resolved_provider=lambda: "fake")
    seen: dict[str, object] = {}

    def fake_run_process_group(cmd, *, cwd, timeout_s):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("bmc_agent.jml_specs.shutil.which", lambda path: "/usr/bin/openjml")
    monkeypatch.setattr("bmc_agent.jml_specs._run_process_group", fake_run_process_group)

    result = run_jml_specs_bench(
        source,
        driver="X",
        config=config,
        llm=llm,
        output_dir=tmp_path / "out",
        openjml_path="openjml",
        openjml_timeout=1,
        max_iterations=1,
    )

    support = Path(result.final_annotated_path).parent / "Verifier.java"
    assert result.passed is True
    assert support.exists()
    assert str(support) in seen["cmd"]


def test_write_openjml_support_files_default_verifier(tmp_path):
    source = """
class X {
    int f() {
        return Verifier.nondetInt();
    }
}
"""
    written = write_openjml_support_files(source, tmp_path)

    assert written == [tmp_path / "Verifier.java"]
    text = (tmp_path / "Verifier.java").read_text(encoding="utf-8")
    assert "public final class Verifier" in text
    assert "public static native int nondetInt()" in text
    assert "ensures condition;" in text


def test_write_openjml_support_files_packaged_svcomp_verifier(tmp_path):
    source = """
import org.sosy_lab.sv_benchmarks.Verifier;

public class Main {
    public static void main(String[] args) {
        String s = Verifier.nondetString();
    }
}
"""
    written = write_openjml_support_files(source, tmp_path)

    expected = tmp_path / "org" / "sosy_lab" / "sv_benchmarks" / "Verifier.java"
    assert written == [expected]
    text = expected.read_text(encoding="utf-8")
    assert text.startswith("package org.sosy_lab.sv_benchmarks;")
    assert "public static native String nondetString()" in text
    assert "ensures \\result != null;" in text


def test_write_openjml_support_files_does_not_duplicate_local_verifier(tmp_path):
    source = """
class Verifier {
    static int nondetInt() { return 0; }
}
class X {
    int f() { return Verifier.nondetInt(); }
}
"""
    assert write_openjml_support_files(source, tmp_path) == []
    assert not (tmp_path / "Verifier.java").exists()


def test_write_openjml_support_files_cookie_shim(tmp_path):
    source = """
public class HttpServletRequest {
    private Cookie cookie = null;
    public Cookie[] getCookies() {
        return new Cookie[] { cookie };
    }
}
"""
    written = write_openjml_support_files(source, tmp_path)

    assert written == [tmp_path / "Cookie.java"]
    text = (tmp_path / "Cookie.java").read_text(encoding="utf-8")
    assert "public class Cookie" in text
    assert "public Cookie(String name, String value)" in text


def test_write_openjml_support_files_does_not_duplicate_local_cookie(tmp_path):
    source = """
class Cookie {}
public class HttpServletRequest {
    private Cookie cookie = null;
}
"""
    assert write_openjml_support_files(source, tmp_path) == []
    assert not (tmp_path / "Cookie.java").exists()


def test_prune_reported_loop_invariant_removes_reported_maintaining():
    source = """
class ContainsDuplicate {
    boolean f(int[] nums) {
        int n = nums.length;
        //@ maintaining 0 <= i && i <= n - 1;
        //@ decreases n - i;
        for (int i = 0; i < n - 1; i++) {
            if (nums[i] == nums[i + 1]) return true;
        }
        return false;
    }
}
"""
    output = "X.java:5: verify: The prover cannot establish an assertion (LoopInvariantBeforeLoop) in method f"
    pruned, changed = _prune_reported_loop_invariant(source, output)
    assert changed
    assert "maintaining 0 <= i" not in pruned
    assert "decreases n - i" in pruned
    assert "for (int i" in pruned


def test_annotate_reported_nullable_marks_local_null_initialization():
    source = """
class ExException {
  static int test(int secret) {
    ExException o = null;
    return 0;
  }
}
"""
    output = "X.java:4: verify: The prover cannot establish an assertion (PossiblyNullInitialization) in method test: o"

    annotated, changed = _annotate_reported_nullable(source, output)

    assert changed
    assert "    //@ nullable\n    ExException o = null;" in annotated
    assert strip_jml_comments(annotated) == strip_jml_comments(source)


def test_annotate_reported_nullable_marks_for_initializer_inline():
    source = """
class LinkedList {
  LinkedListEntry Head;
  int size() {
    int count = 0;
    for (LinkedListEntry entry = Head; entry != null; entry = entry.Next) ++count;
    return count;
  }
}
"""
    output = "X.java:6: verify: The prover cannot establish an assertion (PossiblyNullInitialization) in method size: entry"

    annotated, changed = _annotate_reported_nullable(source, output)

    assert changed
    assert "for (/*@ nullable @*/ LinkedListEntry entry = Head;" in annotated
    assert "//@ nullable\n    for" not in annotated
    preserved, detail = source_code_preserved(source, annotated)
    assert preserved, detail


def test_annotate_reported_nullable_marks_multiple_fields_once():
    source = """
class LinkedListEntry {
  public LinkedListEntry Next;
}
class LinkedList {
  //@ nullable
  public LinkedListEntry Head;
  public LinkedListEntry Tail;
}
"""
    output = "\n".join(
        [
            "X.java:3: verify: The prover cannot establish an assertion (NullField) in method LinkedListEntry",
            "X.java:7: verify: The prover cannot establish an assertion (NullField) in method LinkedList",
            "X.java:8: verify: The prover cannot establish an assertion (NullField) in method LinkedList",
        ]
    )

    annotated, changed = _annotate_reported_nullable(source, output)

    assert changed
    assert annotated.count("/*@ nullable @*/") == 3
    assert "  //@ nullable\n  public LinkedListEntry Head;" not in annotated
    assert "  public /*@ nullable @*/ LinkedListEntry Head;" in annotated
    assert "  public /*@ nullable @*/ LinkedListEntry Tail;" in annotated
    preserved, detail = source_code_preserved(source, annotated)
    assert preserved, detail


def test_normalize_inlines_standalone_nullable_field_annotations():
    source = """
class Node {
  //@ nullable
  public Node next;

  void set(Node n) {
    //@ nullable
    Node local = n;
  }
}
"""

    normalized = normalize_jml_annotation_placement(source)

    assert "public /*@ nullable @*/ Node next;" in normalized
    assert "//@ nullable\n    Node local = n;" in normalized
    preserved, detail = source_code_preserved(source, normalized)
    assert preserved, detail


def test_normalize_splits_compact_class_field_declarations():
    source = "public class HttpServletRequest { private Cookie cookie = null; private String tainted = null;\n}\n"

    normalized = normalize_jml_annotation_placement(source)

    assert "public class HttpServletRequest {\n" in normalized
    assert "private Cookie cookie = null;\n" in normalized
    assert "private String tainted = null;\n" in normalized
    preserved, detail = source_code_preserved(source, normalized)
    assert preserved, detail


def test_annotate_reported_nullable_marks_split_compact_field():
    source = "public class HttpServletRequest { private Cookie cookie = null; private String tainted = null;\n}\n"
    normalized = normalize_jml_annotation_placement(source)
    output = "X.java:2: verify: The prover cannot establish an assertion (NullField) in method HttpServletRequest"

    annotated, changed = _annotate_reported_nullable(normalized, output)

    assert changed
    assert "private /*@ nullable @*/ Cookie cookie = null;" in annotated
    assert "private /*@ nullable @*/ String tainted = null;" not in annotated
    preserved, detail = source_code_preserved(source, annotated)
    assert preserved, detail


def test_has_reported_nullable_failure_only_matches_nullness_diagnostics():
    assert _has_reported_nullable_failure(
        "X.java:2: verify: The prover cannot establish an assertion (NullField)"
    )
    assert _has_reported_nullable_failure(
        "X.java:3: verify: The prover cannot establish an assertion (PossiblyNullInitialization)"
    )
    assert not _has_reported_nullable_failure("openjml wall-clock timeout after 65s")
    assert not _has_reported_nullable_failure(
        "X.java:4: verify: The prover cannot establish an assertion (LoopInvariant)"
    )


def test_annotate_reported_nullable_marks_null_aware_formal_parameter():
    source = """
class Trie {
  private Node get2(Node x, CharArray key, int d) {
    if (x == null) return null;
    return x;
  }
}
"""
    output = (
        "X.java:3: verify: The prover cannot establish an assertion "
        "(NullFormal: X.java:3:) in method get: x in get2(Trie.Node,Trie.CharArray,int)"
    )

    annotated, changed = _annotate_reported_nullable(source, output)

    assert changed
    assert "private Node get2(/*@ nullable @*/ Node x, CharArray key, int d)" in annotated
    preserved, detail = source_code_preserved(source, annotated)
    assert preserved, detail


def test_annotate_reported_nullable_does_not_mark_formal_without_null_handling():
    source = """
class Trie {
  private Node get2(Node x, CharArray key, int d) {
    return x.next[d];
  }
}
"""
    output = (
        "X.java:3: verify: The prover cannot establish an assertion "
        "(NullFormal: X.java:3:) in method get: x in get2(Trie.Node,Trie.CharArray,int)"
    )

    annotated, changed = _annotate_reported_nullable(source, output)

    assert not changed
    assert annotated == source


def test_annotate_reported_nullable_marks_null_return_type():
    source = """
class Trie {
  private Node get2(/*@ nullable @*/ Node x) {
    if (x == null) return null;
    return x;
  }
}
"""
    output = (
        "X.java:3: verify: The prover cannot establish an assertion "
        "(PossiblyNullReturn: X.java:3:) in method get2: get2"
    )

    annotated, changed = _annotate_reported_nullable(source, output)

    assert changed
    assert "private /*@ nullable @*/ Node get2(/*@ nullable @*/ Node x)" in annotated
    preserved, detail = source_code_preserved(source, annotated)
    assert preserved, detail


def test_annotate_reported_nullable_marks_return_type_for_nullable_field_return():
    source = """
class Request {
  private /*@ nullable @*/ String tainted = null;
  public String getHeader() {
    return tainted;
  }
}
"""
    output = (
        "X.java:4: verify: The prover cannot establish an assertion "
        "(PossiblyNullReturn: X.java:4:) in method getHeader: getHeader"
    )

    annotated, changed = _annotate_reported_nullable(source, output)

    assert changed
    assert "public /*@ nullable @*/ String getHeader()" in annotated
    preserved, detail = source_code_preserved(source, annotated)
    assert preserved, detail


def test_annotate_reported_nullable_does_not_mark_return_without_null_return():
    source = """
class Trie {
  private Node get2(Node x) {
    return x;
  }
}
"""
    output = (
        "X.java:3: verify: The prover cannot establish an assertion "
        "(PossiblyNullReturn: X.java:3:) in method get2: get2"
    )

    annotated, changed = _annotate_reported_nullable(source, output)

    assert not changed
    assert annotated == source


def test_normalize_adds_bounds_invariant_for_array_length_loop():
    original = """
class MaxInArray {
    int f(int[] a) {
        for (int i = 0; i < a.length; i++) {
            int x = a[i];
        }
        return 0;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ maintaining 0 <= i && i <= a.length;" in normalized


def test_normalize_adds_bounds_invariant_for_length_alias_loop():
    original = """
class ContainsDuplicate {
    boolean f(int[] nums) {
        int n = nums.length;
        //@ decreases n - i;
        for (int i = 0; i < n - 1; i++) {
            if (nums[i] == nums[i + 1]) return true;
        }
        return false;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ maintaining 0 <= i && i <= n;" in normalized
    assert "//@ maintaining n == nums.length;" in normalized
    assert "//@ decreases n - i;" in normalized


def test_normalize_adds_method_contract_array_bound_invariant():
    original = """
class TspLike {
    int N;
    boolean[] visited;

    /*@ requires visited != null && visited.length == N;
      @*/
    void search() {
        for (int i = 0; i < N; i++) {
            if (visited[i]) continue;
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ maintaining 0 <= i && i <= N;" in normalized
    assert "//@ maintaining visited != null;" in normalized
    assert "//@ maintaining visited.length == N;" in normalized


def test_normalize_does_not_leak_method_contract_array_bounds():
    original = """
class TwoMethods {
    int N;
    boolean[] visited;

    /*@ requires visited != null && visited.length == N;
      @*/
    void first() {
    }

    void second() {
        for (int i = 0; i < N; i++) {
            if (visited[i]) continue;
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ maintaining 0 <= i && i <= N;" not in normalized
    assert "//@ maintaining visited != null;" not in normalized
    assert "//@ maintaining visited.length == N;" not in normalized


def test_normalize_adds_assignable_nothing_for_obviously_pure_helper():
    original = """
class Helper {
    /*@ requires n >= 0;
      @ ensures \\result == n;
      @*/
    int id(int n) {
        return n;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable \\nothing;" in normalized


def test_normalize_does_not_add_assignable_nothing_for_side_effect_method():
    original = """
class Helper {
    int state;

    /*@ requires n >= 0;
      @*/
    void set(int n) {
        state = n;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable \\nothing;" not in normalized


def test_normalize_infers_assignable_frame_for_obvious_field_writes():
    original = """
class SearchLike {
    int best;
    int nCalls;
    boolean[] visited;

    /*@ requires visited != null;
      @*/
    void search(int n) {
        nCalls++;
        best = n;
        for (int i = 0; i < n; i++) {
            visited[i] = true;
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable best, nCalls, visited[*];" in normalized


def test_normalize_inferred_assignable_ignores_local_writes():
    original = """
class LocalOnly {
    /*@ requires n >= 0;
      @*/
    int f(int n) {
        int x = n;
        x++;
        return x;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable x;" not in normalized


def test_normalize_does_not_infer_param_array_frame_when_local_escapes_to_call():
    original = """
class MergeLike {
    //@ requires a != null;
    public static void sort(int[] a) {
        int[] aux = new int[a.length];
        merge(a, aux);
        if (a != aux) {
            for (int i = 0; i < a.length; i++) a[i] = aux[i];
        }
    }

    //@ assignable aux[*];
    private static void merge(int[] a, int[] aux) {
        aux[0] = a[0];
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)

    assert "//@ assignable a[*];\n    public static void sort" not in normalized


def test_normalize_prunes_assignable_multi_declarator_local():
    original = """
class Copy {
    //@ assignable a[*], to;
    void f(int[] a) {
        int[] from = a, to = new int[a.length];
        to[0] = from[0];
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)

    assert "//@ assignable a[*];" in normalized
    assert "assignable a[*], to" not in normalized


def test_normalize_inferred_assignable_ignores_local_receiver_field_writes():
    original = """
class Node {
    Node next;
}
class List {
    Node head;

    //@ requires head != null;
    void link(Node node) {
        Node entry = head;
        entry.next = node;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "assignable next" not in normalized


def test_normalize_inferred_assignable_keeps_this_field_writes():
    original = """
class Node {
    Node next;

    //@ requires node != null;
    void link(Node node) {
        this.next = node;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable next;" in normalized


def test_normalize_adds_constant_array_index_precondition_for_field_array():
    original = """
class Solver {
    private boolean visited[];

    public int solve() {
        visited[0] = true;
        return 0;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires visited != null && visited.length > 0;" in normalized
    assert "private /*@ spec_public @*/ boolean visited[];" in normalized


def test_normalize_skips_constant_array_index_precondition_when_source_guards_access():
    original = """
class Solver {
    boolean[] visited;

    int solve() {
        if (visited == null || visited.length <= 0) return 0;
        visited[0] = true;
        return 1;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires visited != null && visited.length > 0;" not in normalized


def test_normalize_skips_constant_array_index_precondition_for_local_array():
    original = """
class LocalArray {
    int f() {
        int[] values = new int[1];
        values[0] = 1;
        return values[0];
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires values != null && values.length > 0;" not in normalized
    assert "assignable values" not in normalized


def test_normalize_marks_private_fields_spec_public_when_public_contract_references_them():
    original = """
class Solver {
    private int best;

    //@ assignable best;
    public void solve() {
        best = 1;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "private /*@ spec_public @*/ int best;" in normalized


def test_normalize_marks_private_field_with_trailing_comment_spec_public():
    original = """
class TrieLike {
    private Node root; // root of trie

    //@ assignable root;
    public void put(Node node) {
        root = node;
    }
}
class Node {}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "private /*@ spec_public @*/ Node root;" in normalized


def test_normalize_propagates_private_callee_preconditions_without_local_dependencies():
    original = """
class Solver {
    private int N;
    private boolean visited[];

    public void solve() {
        visited[0] = true;
        search(0, N - 1);
    }

    //@ requires N >= 0;
    //@ requires visited != null && visited.length == N;
    //@ requires 0 <= src && src < N;
    //@ requires nLeft >= 0;
    private void search(int src, int nLeft) {
        visited[src] = true;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires N >= 0;" in normalized
    assert "//@ requires visited != null && visited.length == N;" in normalized
    assert "//@ requires 0 <= (0) && (0) < N;" in normalized
    assert "//@ requires (N - 1) >= 0;" in normalized
    assert "private /*@ spec_public @*/ int N;" in normalized
    assert "private /*@ spec_public @*/ boolean visited[];" in normalized


def test_normalize_private_callee_precondition_propagation_preserves_length_property():
    original = """
class LengthName {
    private int[] values;

    public void f() {
        h(0);
    }

    //@ requires values != null && values.length > length;
    private void h(int length) {
        values[length] = 1;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires values != null && values.length > (0);" in normalized
    assert "values.(0)" not in normalized


def test_normalize_does_not_propagate_private_callee_preconditions_into_private_callers():
    original = """
class RecursiveLike {
    private int bound;

    public void start() {
        helper(bound);
    }

    //@ requires n >= 0;
    private void helper(int n) {
        if (n > 0) helper(n - 1);
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires (bound) >= 0;" in normalized
    assert "//@ requires (n - 1) >= 0;" not in normalized


def test_normalize_propagates_private_callee_field_assignable_to_caller():
    original = """
class Solver {
    private int best;
    private int nCalls;
    private boolean visited[];

    //@ assignable best, visited[*];
    public void solve() {
        search();
    }

    //@ assignable best, nCalls, visited[*];
    private void search() {
        nCalls++;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable nCalls;" in normalized


def test_normalize_does_not_propagate_private_callee_parameter_assignable_to_caller():
    original = """
class MergeLike {
    //@ assignable a[*];
    public static void sort(int[] a) {
        int[] aux = new int[a.length];
        merge(a, aux);
    }

    //@ assignable a[*], aux[*];
    private static void merge(int[] a, int[] aux) {
        aux[0] = a[0];
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable a[*], aux[*];\n    public static void sort" not in normalized


def test_normalize_prunes_assignable_locations_outside_method_scope():
    original = """
class Entry {
    public Entry Next;
    public int Value;
}
class LinkedList {
    public Entry Head;

    //@ assignable Head, Next, Value;
    public void add(int e) {
        Entry entry = Head;
        entry.Next = new Entry();
        Head = entry;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable Head;" in normalized
    assert "assignable Head, Next" not in normalized
    assert "assignable Next" not in normalized
    assert "assignable Value" not in normalized


def test_normalize_keeps_assignable_parameter_array_frame():
    original = """
class Fill {
    //@ assignable a[*];
    public void fill(int[] a) {
        a[0] = 1;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ assignable a[*];" in normalized


def test_normalize_adds_branch_conditioned_nullable_receiver_precondition():
    original = """
class Node {
    public int x;
    public /*@ nullable @*/ Node next;

    public void insert(int data) {
        if (data > this.x) {
            next.insert(data);
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires data > this.x ==> next != null;" in normalized


def test_normalize_skips_branch_nullable_receiver_when_condition_guards_nonnull():
    original = """
class Node {
    public /*@ nullable @*/ Node next;

    public void visit() {
        if (next != null) {
            next.visit();
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "==> next != null" not in normalized


def test_normalize_skips_branch_nullable_receiver_when_condition_uses_local():
    original = """
class Node {
    public int x;
    public /*@ nullable @*/ Node next;

    public void visit(int data) {
        int pivot = this.x;
        if (data > pivot) {
            next.visit(data);
        }
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires data > pivot ==> next != null;" not in normalized


def test_normalize_does_not_propagate_private_callee_preconditions_with_loop_locals():
    original = """
class MergeLike {
    public static void sort(int[] a) {
        for (int start = 0; start < a.length; start++) {
            merge(a, start);
        }
    }

    //@ requires start <= a.length;
    private static void merge(int[] a, int start) {
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires (start) <= (a).length;" not in normalized


def test_normalize_keeps_method_contracts_that_reference_fields():
    original = """
class FieldContract {
    private int limit;

    //@ requires limit >= 0;
    //@ ensures \\result >= limit;
    public int f() {
        int local = limit;
        return local;
    }

    //@ ensures \\result == local;
    public int bad() {
        int local = 1;
        return local;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires limit >= 0;" in normalized
    assert "//@ ensures \\result >= limit;" in normalized
    assert "//@ ensures \\result == local;" not in normalized


def test_normalize_adds_bounds_invariant_for_inclusive_length_alias_loop():
    original = """
class ThreeConsecutiveOdds {
    boolean f(int[] arr) {
        int n = arr.length;
        for (int i = 0; i <= n - 3; ++i) {
            if (arr[i] == arr[i + 2]) return true;
        }
        return false;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ maintaining 0 <= i && i <= n;" in normalized
    assert "//@ maintaining n == arr.length;" in normalized


def test_normalize_drops_redundant_length_lower_bound_for_guarded_loop():
    original = """
class ThreeConsecutiveOdds {
    //@ requires arr.length >= 3;
    boolean f(int[] arr) {
        int n = arr.length;
        for (int i = 0; i <= n - 3; ++i) {
            if (arr[i] == arr[i + 2]) return true;
        }
        return false;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "requires arr.length >= 3" not in normalized
    assert "//@ maintaining 0 <= i && i <= n;" in normalized
    assert "//@ maintaining n == arr.length;" in normalized


def test_normalize_relaxes_too_strong_inclusive_loop_bound_invariant():
    original = """
class WindowScan {
    boolean f(int[] arr) {
        int n = arr.length;
        //@ maintaining 0 <= i && i <= n - 2;
        //@ decreases n - i;
        for (int i = 0; i <= n - 3; ++i) {
            if (arr[i] == arr[i + 2]) return true;
        }
        return false;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ maintaining 0 <= i && i <= n;" in normalized
    assert "i <= n - 2" not in normalized
    assert "//@ decreases n - i;" in normalized


def test_normalize_keeps_length_lower_bound_without_guarded_loop():
    original = """
class FirstThree {
    //@ requires arr.length >= 3;
    int f(int[] arr) {
        return arr[2];
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "requires arr.length >= 3" in normalized


def test_normalize_drops_guarded_string_length_upper_bound_conjunct():
    original = """
class IsAllUnique {
    //@ requires str.length() <= 26 && (\\forall int i; 0 <= i && i < str.length(); 'a' <= str.charAt(i) && str.charAt(i) <= 'z');
    boolean isAllUnique(String str) {
        int len = str.length();
        if (len > 26) {
            return false;
        }
        return true;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "requires str.length() <= 26" not in normalized
    assert "'a' <= str.charAt(i)" in normalized
    assert "'z'" in normalized


def test_normalize_keeps_unguarded_string_length_upper_bound():
    original = """
class Prefix {
    //@ requires s.length() <= 4 && s != null;
    int f(String s) {
        return s.length();
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "requires s.length() <= 4 && s != null;" in normalized


def test_normalize_drops_orphan_block_continuation_after_complete_clause():
    original = """
class CheckABeforeB {
    /*@ requires s != null;
      @     (\\forall int p; 0 <= p && p < k; s.charAt(p) == 'a') &&
      @     (\\forall int q; k <= q && q < s.length(); s.charAt(q) == 'b'));
      @*/
    boolean checkString(String s) {
        return true;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "requires s != null;" in normalized
    assert "p < k" not in normalized
    assert "q < s.length" not in normalized


def test_normalize_comments_bare_jml_clause_lines():
    original = """
class IsAllUnique {
      requires str != null;
      ensures \\result ==> (\\forall int i; 0 <= i && i < str.length();
    boolean isAllUnique(String str) {
        return true;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires str != null;" in normalized
    assert "      requires str != null;" not in normalized
    assert "ensures \\result ==>" not in normalized
    assert "boolean isAllUnique" in normalized


def test_normalize_comments_bare_block_continuation_jml_clause_lines():
    original = """
class Solver {
    @ requires visited != null;
    @ assignable \\everything;
    int solve(int[] visited) {
        return 0;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "    //@ requires visited != null;" in normalized
    assert "    //@ assignable \\everything;" in normalized
    assert "    @ requires" not in normalized
    assert "int solve(int[] visited)" in normalized


def test_normalize_does_not_comment_java_assert_statement():
    original = """
class X {
    void f() {
        assert false;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "assert false;" in normalized
    assert "//@ assert false;" not in normalized


def test_normalize_does_not_comment_java_assume_call():
    original = """
class X {
    void assume(boolean b) {}
    void f(int size) {
        assume(size >= 0);
        byte[] bytes = new byte[size];
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "assume(size >= 0);" in normalized
    assert "//@ assume" not in normalized


def test_normalize_drops_prebound_quantifier_variable_use():
    original = """
class ThreeConsecutiveOdds {
    //@ ensures i <= arr.length - 3 ==> \\result == (\\exists int i; 0 <= i; arr[i] == 1);
    boolean threeConsecutiveOdds(int[] arr) {
        return false;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "ensures i <=" not in normalized
    assert "boolean threeConsecutiveOdds" in normalized


def test_normalize_drops_prebound_multiline_clause_as_unit():
    original = """
class ThreeConsecutiveOdds {
    //@ ensures i <= arr.length - 3 ==> \\result == (\\exists int i; 0 <= i;
    //@     arr[i] == 1);
    boolean threeConsecutiveOdds(int[] arr) {
        return false;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "ensures i <=" not in normalized
    assert "arr[i] == 1" not in normalized
    assert "boolean threeConsecutiveOdds" in normalized


def test_normalize_drops_result_postcondition_on_void_method():
    original = """
class ExMIT {
    //@ ensures \\result == 0;
    public static void main(String[] args) {
        return;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "ensures \\result" not in normalized
    assert "public static void main" in normalized


def test_normalize_drops_constructor_assignable_clause():
    original = """
class MinePump {
    Environment env;
    //@ requires env != null;
    //@ assignable this.env, pumpRunning;
    public MinePump(Environment env) {
        this.env = env;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "requires env != null" in normalized
    assert "assignable this.env" not in normalized
    assert "public MinePump(Environment env)" in normalized


def test_normalize_drops_method_only_clause_before_field_declaration():
    original = """
class AlarmOutputs {
  //@ public model int isAudioDisabled;
  //@ assignable isAudioDisabled;
  public int isAudioDisabled = 0;
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "public model int isAudioDisabled" not in normalized
    assert "assignable isAudioDisabled" not in normalized
    assert "public int isAudioDisabled = 0;" in normalized


def test_normalize_adds_nonzero_requires_for_param_division():
    original = """
class Div {
    //@ ensures \\result == a / b;
    int div(int a, int b) {
        return a / b;
    }
}
"""
    normalized = normalize_jml_annotation_placement(original)
    assert "//@ requires b != 0;" in normalized


def test_transplant_jml_preserves_original_method_modifiers():
    original = """
public class Biggest {
    static public int biggest(int[] a) {
        while (a.length > 0) {
            return 0;
        }
        return -1;
    }
}
"""
    annotated = """
public class Biggest {
    //@ requires a.length > 0;
    //@ ensures 0 <= \\result;
    public static int biggest(int[] a) {
        //@ maintaining a.length > 0;
        //@ decreases a.length;
        while (a.length > 0) {
            return 0;
        }
        return -1;
    }
}
"""
    transplanted = transplant_jml_annotations(original, annotated)
    assert transplanted is not None
    assert "static public int biggest" in transplanted
    assert "public static int biggest" not in transplanted
    assert "//@ requires a.length > 0;" in transplanted
    assert "//@ maintaining a.length > 0;" in transplanted
    ok, err = source_code_preserved(original, transplanted)
    assert ok, err


def test_transplant_jml_returns_none_without_matching_targets():
    original = "class X { int f() { return 1; } }\n"
    annotated = """
class X {
    //@ ensures \\result == 2;
    int g() { return 2; }
}
"""
    assert transplant_jml_annotations(original, annotated) is None


def test_complete_standard_imports_adds_missing_java_util_import():
    original = """
import java.util.HashSet;

class RepeatedChar {
    char f(String s) {
        Set<Character> seen = new HashSet<Character>();
        return ' ';
    }
}
"""
    completed = complete_standard_imports(original)
    assert "import java.util.Set;" in completed
    assert "import java.util.HashSet;" in completed


def test_complete_standard_imports_does_not_import_declared_nested_type():
    original = """
import java.util.List;

public class SortedListInsert {
  private static class List {
    List next;
  }
}
"""
    completed = complete_standard_imports(original)

    assert "import java.util.List;" not in completed
    assert "private static class List" in completed


def test_drop_generated_jml_assertions_preserves_input_asserts_only():
    original = """
class X {
    void f(int x) {
        //@ assert x >= 0;
    }
}
"""
    annotated = """
class X {
    void f(int x) {
        //@ assert x >= 0;
        //@ assert x < 10;
    }
}
"""
    cleaned = drop_generated_jml_assertions(original, annotated)
    assert "//@ assert x >= 0;" in cleaned
    assert "//@ assert x < 10;" not in cleaned


def test_drop_generated_jml_assertions_removes_generated_assumes_only():
    original = """
class X {
    void f(int x) {
        //@ assume x >= 0;
    }
}
"""
    annotated = """
class X {
    void f(int x) {
        //@ assume x >= 0;
        //@ assume x < 10;
        //@ assert x > -1;
    }
}
"""
    cleaned = drop_generated_jml_assertions(original, annotated)
    assert "//@ assume x >= 0;" in cleaned
    assert "//@ assume x < 10;" not in cleaned
    assert "//@ assert x > -1;" not in cleaned


def test_drop_generated_jml_assertions_handles_call_syntax_without_space():
    original = """
class X {
    void f(int x) {
        //@ assume(x >= 0);
    }
}
"""
    annotated = """
class X {
    void f(int x) {
        //@ assume(x >= 0);
        //@ assume(x < 10);
        //@ assert(x > -1);
    }
}
"""
    cleaned = drop_generated_jml_assertions(original, annotated)
    assert "//@ assume(x >= 0);" in cleaned
    assert "//@ assume(x < 10);" not in cleaned
    assert "//@ assert(x > -1);" not in cleaned


def test_drop_generated_jml_assertions_handles_single_line_block_statements():
    original = """
class X {
    void f(int x) {
        /*@ assume x >= 0; @*/
    }
}
"""
    annotated = """
class X {
    void f(int x) {
        /*@ assume x >= 0; @*/
        /*@ assume x < 10; @*/
        /*@ assert x > -1; @*/
        /*@ ensures true; @*/
    }
}
"""
    cleaned = drop_generated_jml_assertions(original, annotated)
    assert "/*@ assume x >= 0; @*/" in cleaned
    assert "/*@ assume x < 10; @*/" not in cleaned
    assert "/*@ assert x > -1; @*/" not in cleaned
    assert "/*@ ensures true; @*/" in cleaned


def test_source_preservation_allows_only_standard_import_additions():
    original = """
import java.util.HashSet;
class X {
    void f() { Set<String> s = new HashSet<String>(); }
}
"""
    with_import = """
import java.util.HashSet;
import java.util.Set;
class X {
    void f() { Set<String> s = new HashSet<String>(); }
}
"""
    with_modifier_change = """
import java.util.HashSet;
import java.util.Set;
class X {
    public void f() { Set<String> s = new HashSet<String>(); }
}
"""
    ok, err = source_code_preserved_with_standard_imports(original, with_import)
    assert ok, err
    ok, err = source_code_preserved_with_standard_imports(original, with_modifier_change)
    assert not ok
    assert "executable Java code" in err


def test_prune_reported_postcondition_uses_contract_location():
    source = """class X {
  //@ ensures \\result > 0;
  int f() {
    return -1;
  }
}
"""
    output = (
        "/tmp/X.java:4: verify: The prover cannot establish an assertion "
        "(Postcondition: /tmp/X.java:2:) in method f\n"
        "    return -1;\n"
        "    ^\n"
        "/tmp/X.java:2: verify: Associated declaration: /tmp/X.java:4:\n"
        "  //@ ensures \\result > 0;\n"
        "      ^\n"
    )

    pruned, changed = _prune_reported_postcondition(source, output)

    assert changed
    assert "ensures" not in pruned
    assert "return -1;" in pruned


def test_prune_reported_precondition_removes_false_requires_conjunct():
    source = """class Sorted {
  /*@ requires this.x < Integer.MAX_VALUE ==> this.next != null;
    @ requires data >= 0;
    @ assignable \\everything;
    @*/
  void insert(int data) {
    next.insert(data);
  }
}
"""
    output = (
        "/tmp/Sorted.java:7: verify: The prover cannot establish an assertion "
        "(Precondition: /tmp/Sorted.java:5:) in method insert\n"
        "/tmp/Sorted.java:2: verify: Precondition conjunct is false: "
        "this.x < Integer.MAX_VALUE ==> this.next != null\n"
        "  /*@ requires this.x < Integer.MAX_VALUE ==> this.next != null;\n"
    )

    pruned, changed = _prune_reported_precondition(source, output)

    assert changed
    assert "this.x < Integer.MAX_VALUE" not in pruned
    assert "requires data >= 0" in pruned
    assert "assignable \\everything" in pruned


def test_prune_reported_assignable_removes_reported_frame_clause():
    source = """class TspSolver {
  /*@ requires N >= 0;
    @ assignable best, visited[*], nCalls;
    @*/
  void search() {
    bound();
  }
}
"""
    output = (
        "/tmp/TspSolver.java:5: verify: The prover cannot establish an assertion "
        "(Assignable: /tmp/TspSolver.java:3:) in method search: \\everything\n"
        "/tmp/TspSolver.java:3: verify: Associated declaration: /tmp/TspSolver.java:5:\n"
        "    @ assignable best, visited[*], nCalls;\n"
    )

    pruned, changed = _prune_reported_assignable(source, output)

    assert changed
    assert "assignable best" not in pruned
    assert "requires N >= 0" in pruned
    assert "void search()" in pruned


def test_prune_reported_assignable_preserves_multiline_block():
    source = """class Node {
  /*@ assignable static_next, this.next;
    @ ensures \\result != null;
    @*/
  Node swapNode() { return this; }
}
"""
    output = (
        "/tmp/Node.java:5: verify: The prover cannot establish an assertion "
        "(Assignable: /tmp/Node.java:2:) in method swapNode: t.next\n"
        "/tmp/Node.java:2: verify: Associated declaration: /tmp/Node.java:5:\n"
        "  /*@ assignable static_next, this.next;\n"
    )

    pruned, changed = _prune_reported_assignable(source, output)

    assert changed
    assert "assignable static_next" not in pruned
    assert "/*@\n    @ ensures \\result != null;" in pruned
    assert "Node swapNode()" in pruned


def test_prune_reported_assignable_removes_multiple_reported_frame_clauses():
    source = """class X {
  //@ assignable a[*];
  void f(int[] a) { a[0] = 1; }

  //@ assignable b[*];
  void g(int[] b) { b[0] = 1; }
}
"""
    output = (
        "/tmp/X.java:3: verify: The prover cannot establish an assertion "
        "(Assignable: /tmp/X.java:2:) in method f: a[*]\n"
        "/tmp/X.java:6: verify: The prover cannot establish an assertion "
        "(Assignable: /tmp/X.java:5:) in method g: b[*]\n"
    )

    pruned, changed = _prune_reported_assignable(source, output)

    assert changed
    assert "assignable a" not in pruned
    assert "assignable b" not in pruned
    assert "void f" in pruned
    assert "void g" in pruned


def test_prune_reported_diverges_removes_only_reported_clause():
    source = """class Verifier {
  //@ requires true;
  //@ assignable \\everything;
  //@ diverges !condition;
  //@ ensures condition;
  static void assume(boolean condition) {
    if (!condition) Runtime.getRuntime().halt(1);
  }
}
"""
    output = (
        "/tmp/Verifier.java:4: verify: The prover cannot establish an assertion "
        "(Diverges: /tmp/Verifier.java:7:) in method f\n"
        "  //@ diverges !condition;\n"
    )

    pruned, changed = _prune_reported_diverges(source, output)

    assert changed
    assert "diverges" not in pruned
    assert "requires true" in pruned
    assert "assignable \\everything" in pruned
    assert "ensures condition" in pruned


def test_prune_reported_loop_invariant_removes_multiple_reported_clauses():
    source = """class X {
  void f(int n) {
    //@ maintaining i >= 0;
    //@ maintaining i <= n;
    for (int i = 0; i < n; i++) {}
  }
}
"""
    output = (
        "/tmp/X.java:3: verify: The prover cannot establish an assertion (LoopInvariant) in method f\n"
        "/tmp/X.java:4: verify: The prover cannot establish an assertion (LoopInvariantBeforeLoop) in method f\n"
    )

    pruned, changed = _prune_reported_loop_invariant(source, output)

    assert changed
    assert "maintaining i >= 0" not in pruned
    assert "maintaining i <= n" not in pruned
    assert "for (int i" in pruned


def test_prune_reported_object_invariant_removes_public_invariant():
    source = """class CharArray {
  //@ public invariant array != null && array.length == length;
  char[] array;
  char get(int i) { return array[i]; }
}
"""
    output = (
        "/tmp/Trie.java:4: verify: The prover cannot establish an assertion "
        "(InvariantEntrance: /tmp/Trie.java:2:) in method substring\n"
        "/tmp/Trie.java:2: verify: Associated declaration: /tmp/Trie.java:4:\n"
        "  //@ public invariant array != null && array.length == length;\n"
    )

    pruned, changed = _prune_reported_object_invariant(source, output)

    assert changed
    assert "public invariant" not in pruned
    assert "char get" in pruned


def test_prune_reported_loop_decreases_removes_only_variant():
    source = """class DigitRoot {
  int f(int num) {
    /*@ maintaining num >= 0;
      @ decreases num + (num >= 10 ? 1 : 0);
      @*/
    while (num >= 10) {
      num = num / 10;
    }
    return num;
  }
}
"""
    output = (
        "/tmp/DigitRoot.java:4: verify: The prover cannot establish an assertion "
        "(LoopDecreases) in method f\n"
        "      @ decreases num + (num >= 10 ? 1 : 0);\n"
        "        ^\n"
    )

    pruned, changed = _prune_reported_loop_decreases(source, output)

    assert changed
    assert "decreases" not in pruned
    assert "maintaining num >= 0" in pruned
    assert "while (num >= 10)" in pruned


def test_build_openjml_command_shape():
    cmd = build_openjml_command("openjml", "X.java", 33)
    assert cmd[0] == "openjml"
    assert "--esc" in cmd
    assert "--prover=cvc4" in cmd
    assert "--code-math=java" in cmd
    assert "--timeout" in cmd and "33" in cmd
    assert cmd[-1] == "X.java"


def test_build_openjml_command_appends_support_sources():
    cmd = build_openjml_command(
        "openjml",
        "Main.java",
        33,
        ["org/sosy_lab/sv_benchmarks/Verifier.java", "Cookie.java"],
    )

    assert cmd[-3:] == ["Main.java", "org/sosy_lab/sv_benchmarks/Verifier.java", "Cookie.java"]


def test_run_openjml_compiles_packaged_verifier_support(tmp_path: Path):
    src = tmp_path / "Main.java"
    src.write_text("import org.sosy_lab.sv_benchmarks.Verifier;\npublic class Main {}\n")
    support = tmp_path / "org" / "sosy_lab" / "sv_benchmarks" / "Verifier.java"
    support.parent.mkdir(parents=True)
    support.write_text("package org.sosy_lab.sv_benchmarks; public class Verifier {}\n")
    seen: dict[str, object] = {}

    def fake_run_process_group(cmd, *, cwd, timeout_s):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["timeout_s"] = timeout_s
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch("bmc_agent.jml_specs._run_process_group", side_effect=fake_run_process_group):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9, cwd=tmp_path)

    assert result.passed is True
    assert seen["cwd"] == str(tmp_path)
    assert str(support) in seen["cmd"]
    assert seen["cmd"][-2:] == [str(src), str(support)]


def test_run_openjml_discovers_support_from_source_parent_when_cwd_differs(tmp_path: Path):
    case_dir = tmp_path / "case"
    iter_dir = case_dir / "iter_1"
    iter_dir.mkdir(parents=True)
    src = iter_dir / "X.java"
    src.write_text("class X { int f() { return Verifier.nondetInt(); } }\n")
    support = iter_dir / "Verifier.java"
    support.write_text("public final class Verifier { public static native int nondetInt(); }\n")
    seen: dict[str, object] = {}

    def fake_run_process_group(cmd, *, cwd, timeout_s):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch("bmc_agent.jml_specs._run_process_group", side_effect=fake_run_process_group):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9, cwd=case_dir)

    assert result.passed is True
    assert seen["cwd"] == str(case_dir)
    assert seen["cmd"][-2:] == [str(src), str(support)]


def test_run_openjml_resolves_existing_relative_source_when_cwd_is_set(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    source_dir = work / "artifacts" / "case"
    source_dir.mkdir(parents=True)
    src = source_dir / "X.java"
    src.write_text("class X { int f() { return Verifier.nondetInt(); } }\n")
    support = source_dir / "Verifier.java"
    support.write_text("public final class Verifier { public static native int nondetInt(); }\n")
    seen: dict[str, object] = {}

    def fake_run_process_group(cmd, *, cwd, timeout_s):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.chdir(work)
    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch("bmc_agent.jml_specs._run_process_group", side_effect=fake_run_process_group):
        result = run_openjml(Path("artifacts/case/X.java"), openjml_path="openjml", timeout_s=9, cwd=source_dir)

    assert result.passed is True
    assert seen["cwd"] == str(source_dir)
    assert seen["cmd"][-2:] == [str(src.resolve()), str(support.resolve())]


def test_run_openjml_pass_requires_empty_output(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text("public class X {}\n")

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch(
             "bmc_agent.jml_specs._run_process_group",
             return_value=subprocess.CompletedProcess(["openjml"], 0, "", ""),
         ):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9)
    assert result.passed is True
    assert result.status == "passed"


def test_run_openjml_nonempty_output_is_verification_failure(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text("public class X {}\n")

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch(
             "bmc_agent.jml_specs._run_process_group",
             return_value=subprocess.CompletedProcess(
                 ["openjml"],
                 0,
                 "X.java:2: verify: The prover cannot establish an assertion",
                 "",
             ),
         ):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9)
    assert result.passed is False
    assert result.status == "verification_failed"


def test_run_openjml_classifies_source_frontend_errors(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text("static class X {}\n")

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch(
             "bmc_agent.jml_specs._run_process_group",
             return_value=subprocess.CompletedProcess(
                 ["openjml"],
                 1,
                 "X.java:1: error: modifier static not allowed here\n",
                 "",
             ),
         ):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9)
    assert result.passed is False
    assert result.status == "source_invalid"


def test_run_openjml_keeps_jml_syntax_errors_as_annotation_errors(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text("public class X {}\n")

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch(
             "bmc_agent.jml_specs._run_process_group",
             return_value=subprocess.CompletedProcess(
                 ["openjml"],
                 1,
                 "X.java:2: error: Signals clauses are not permitted in normal specification cases\n",
                 "",
             ),
         ):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9)
    assert result.passed is False
    assert result.status == "annotation_error"


def test_run_openjml_timeout_reports_wall_clock_timeout(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text("public class X {}\n")
    timeout = subprocess.TimeoutExpired(["openjml"], 14, output="out", stderr="err")

    with patch("bmc_agent.jml_specs.shutil.which", return_value="/usr/bin/openjml"), \
         patch("bmc_agent.jml_specs._run_process_group", side_effect=timeout):
        result = run_openjml(src, openjml_path="openjml", timeout_s=9)
    assert result.passed is False
    assert result.status == "timeout"
    assert "wall-clock timeout" in result.error
