#!/usr/bin/env python3
"""oss-bmc — general OSS-Fuzz build-integration + bmc-agent FP-hint generator.

The reusable engine behind the `/oss-bmc` command. For ANY OSS-Fuzz project it:
  1. derives the project's REAL compile flags from its own build system
     (compile_commands.json — cmake gives it for free; make/autotools via compiledb),
  2. runs bmc-agent/CBMC per parser source file with those exact -I/-D flags
     (accuracy fixes C+D active), producing memory_safety counterexamples,
  3. emits the candidate worklist — the "FP-hints": a list of
     (function, property, location) safety-critical dereferences/writes to verify.

Those hints then drive a directed source audit (verify each guard) + ASan
confirmation of any escalation — see .claude/commands/oss-bmc.md.

Usage:
  oss_bmc.py <project> [--files REGEX] [--max-files N] [--list-hints]
  oss_bmc.py <project> --setup-only      # just produce compile_commands.json
"""
import argparse, json, re, shlex, subprocess, sys
from pathlib import Path

CORPORA = Path("/tmp/oss_fuzz_corpora")
ENV = Path.home() / ".config/bmc-agent/env"


def load_env():
    env = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def get_compile_db(proj: Path) -> Path | None:
    """Derive compile_commands.json from the project's own build system."""
    bd = proj / "_cbmc_build"
    cc = bd / "compile_commands.json"
    if cc.exists():
        return cc
    if (proj / "CMakeLists.txt").exists():
        print(f"[oss-bmc] cmake configure {proj.name} ...")
        subprocess.run(
            ["cmake", "-S", str(proj), "-B", str(bd),
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", "-DCMAKE_BUILD_TYPE=Release"],
            capture_output=True)
        if cc.exists():
            return cc
    # make / autotools: intercept with compiledb if present
    for builder in (["compiledb", "-n", "make", "-C", str(proj)],
                    ["bear", "--", "make", "-C", str(proj)]):
        if subprocess.run(["which", builder[0]], capture_output=True).returncode == 0:
            print(f"[oss-bmc] {builder[0]} make {proj.name} ...")
            subprocess.run(builder, capture_output=True, timeout=900)
            alt = proj / "compile_commands.json"
            if alt.exists():
                return alt
    return None


def flags_for(entry: dict):
    args = shlex.split(entry.get("command", "") or " ".join(entry.get("arguments", [])))
    incs, defs = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-I":
            incs.append(args[i + 1]); i += 2
        elif a.startswith("-I"):
            incs.append(a[2:]); i += 1
        elif a == "-D":
            defs.append(args[i + 1]); i += 2
        elif a.startswith("-D"):
            defs.append(a[2:]); i += 1
        else:
            i += 1
    return incs, defs


# files most likely to parse attacker-controlled input
PARSER_HINT = re.compile(r"(pars|read|decod|demux|box|chunk|header|token|scan|"
                         r"depack|t1|t2|j2k|tif_|mpegts|rtp|isom|av_)", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--files", default=None, help="regex to select source files")
    ap.add_argument("--max-files", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=900, help="per-file verify cap (s)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--list-hints", action="store_true",
                    help="just print the FP-hint worklist from existing artifacts")
    args = ap.parse_args()

    proj = CORPORA / args.project
    if not proj.is_dir():
        sys.exit(f"[oss-bmc] project not cloned at {proj}")

    cc = get_compile_db(proj)
    if not cc:
        sys.exit(f"[oss-bmc] could not derive compile_commands.json for {args.project} "
                 f"(no cmake; install compiledb/bear for make/autotools)")
    print(f"[oss-bmc] compile_commands.json: {cc}")
    db = json.loads(cc.read_text())

    out = Path(args.out or f"/tmp/oss_bmc/{args.project}")
    out.mkdir(parents=True, exist_ok=True)

    # choose parser source files
    sel = re.compile(args.files, re.I) if args.files else PARSER_HINT
    targets = []
    for e in db:
        f = e["file"]
        if not f.endswith(".c"):
            continue
        if sel.search(Path(f).name):
            targets.append(e)
    # de-dup by file, prioritise larger (richer) parsers
    seen = {}
    for e in targets:
        seen.setdefault(e["file"], e)
    targets = list(seen.values())[: args.max_files]
    print(f"[oss-bmc] {len(targets)} parser file(s) selected:")
    for e in targets:
        print("   ", Path(e["file"]).name)
    if args.setup_only:
        return

    import os
    env = os.environ.copy()
    env.update(load_env())

    for e in targets:
        src = e["file"]
        incs, defs = flags_for(e)
        # always include the file's own dir + the cmake build dir (generated headers)
        incs = list(dict.fromkeys(incs + [str(Path(src).parent), str(cc.parent)]))
        cmd = ["uv", "run", "python", "-m", "bmc_agent.cli", "verify",
               "--source", src, "--driver", "ossfz",
               "--output", str(out / Path(src).stem),
               "--legacy-spec-gen", "--no-realism-check",
               "--no-dynamic-validation", "--no-feedback-loop",
               "--no-spec-refiner", "--no-inlining-advisor",
               "--no-spec-gen-tools", "--no-realism-tools"]
        for d in incs:
            cmd += ["--include-dir", d]
        for d in defs:
            cmd += ["-D", d]
        log = out / f"{Path(src).stem}.log"
        print(f"[oss-bmc] verify {Path(src).name} (build flags: {len(incs)} -I, {len(defs)} -D) -> {log.name}")
        with log.open("w") as fh:
            subprocess.run(["timeout", str(args.timeout)] + cmd, env=env,
                           stdout=fh, stderr=subprocess.STDOUT)

    emit_hints(out)


def emit_hints(out: Path):
    """Print the memory_safety FP-hint worklist from the verify artifacts."""
    print("\n[oss-bmc] === FP-HINT WORKLIST (memory_safety candidates to audit) ===")
    n = 0
    for br in out.rglob("bug_report.json"):
        try:
            rep = json.loads(br.read_text()).get("report", {})
        except Exception:
            continue
        for c in (rep.get("counterexamples") or []):
            fp = c.get("failing_property", "") or ""
            if ".unwind." in fp:
                continue
            if "pointer" not in fp and "bounds" not in fp and "overflow" not in fp:
                continue
            loc = c.get("failure_location") or {}
            print(f"  {rep.get('function_name','?'):30s} | {fp:42s} | "
                  f"{Path(loc.get('file','?')).name}:{loc.get('line','?')}")
            n += 1
    print(f"[oss-bmc] {n} hint(s). Next: directed source audit (verify each guard) + ASan confirm.")


if __name__ == "__main__":
    main()
