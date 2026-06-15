#!/usr/bin/env python3
"""Deterministic GO/NO-GO for the realism discipline-rule re-validation.

Module-by-module verification scoping matters: a primitive (memcpy/memset/strncpy)
verified in its OWN module (string.c) has NO in-corpus caller -- the dangerous
callers live in OTHER modules (elf.c, vfs.c). So under the discipline rule, string
primitives CORRECTLY demote (no caller present); the real primitive bugs surface in
the CALLER's module (elf). Therefore:

  PASS  = (string) strncpy DEMOTED  [FP closed; all string primitives should demote]
          AND (dtb) read_be64 KEPT  [real OOB via dtb_parse ENTRY POINT must survive]
          AND (elf) >=1 real parser OOB KEPT (elf_validate/elf_calc_size/
              elf_process_relocations)  [entry-point/in-corpus reals must survive]
          AND no budget/400 contamination in any arm.
  OVERTIGHTEN = read_be64 demoted, or ALL elf parser reals demoted (rule kills real bugs).
  INSUFFICIENT = strncpy still confirmed in string (FP not closed).
Exit 0 only on PASS. Appends a decision block to findings/JUDGMENT_NOTES.md.
"""
import json, glob, os, sys
ROOT = os.path.expanduser("~/AProver")
ELF_REALS = ("elf_validate", "elf_calc_size", "elf_process_relocations", "elf_entry", "elf_load_at")

def latest(p):
    ds = sorted(glob.glob(os.path.join(ROOT, "findings", p)), reverse=True)
    return ds[0] if ds else None

def contaminated(d):
    try:
        s = open(os.path.join(d, "run.log"), errors="replace").read()
    except OSError:
        return True
    return ("workspace API usage" in s) or ("Error code: 400" in s) or ("Realism check LLM call failed" in s)

def confirmed(d):
    out = {}
    for f in glob.glob(d + "/**/bug_reports/*.json", recursive=True):
        try:
            r = json.load(open(f)).get("report", {})
        except Exception:
            continue
        if str(r.get("confidence", "")).startswith("confirmed"):
            out.setdefault(r.get("function_name", "?"), set()).add(r.get("violated_property", "?"))
    return out

def main():
    sd, dd, ed = latest("judge_disc_string_*/"), latest("judge_disc_dtb_*/"), latest("judge_disc_elf_*/")
    for name, d in (("string", sd), ("dtb", dd), ("elf", ed)):
        if not d or not os.path.exists(d + "/DONE"):
            print("INCOMPLETE:", name, "not finished"); return 2
    for name, d in (("string", sd), ("dtb", dd), ("elf", ed)):
        if contaminated(d):
            print("CONTAMINATED:", name, "-> invalid (budget/400/realism-fail)"); return 3
    scon, dcon, econ = confirmed(sd), confirmed(dd), confirmed(ed)
    strncpy_confirmed = bool(scon.get("strncpy"))
    readbe64_kept = any("read_be64" in p for p in dcon.get("read_be64", set()))
    elf_real_count = sum(len(econ.get(fn, set())) for fn in ELF_REALS)
    verdict, reasons = "PASS", []
    if not readbe64_kept:
        verdict = "OVERTIGHTEN"; reasons.append("dtb read_be64 (real entry-point OOB) DEMOTED")
    if elf_real_count == 0:
        verdict = "OVERTIGHTEN"; reasons.append("ALL elf parser reals demoted (rule kills real bugs)")
    if strncpy_confirmed and verdict == "PASS":
        verdict = "INSUFFICIENT"; reasons.append("strncpy still CONFIRMED in string (FP not closed)")
    if verdict == "PASS":
        reasons.append("strncpy demoted (FP closed); read_be64 kept; elf reals kept (%d confirmed)" % elf_real_count)
    print("VERDICT:", verdict)
    print("string confirmed:", {k: sorted(v) for k, v in scon.items()})
    print("dtb confirmed:", {k: sorted(v) for k, v in dcon.items()})
    print("elf confirmed:", {k: sorted(v) for k, v in econ.items()})
    print("reasons:", "; ".join(reasons))
    with open(os.path.join(ROOT, "findings", "JUDGMENT_NOTES.md"), "a") as fh:
        fh.write("\n## OVERNIGHT DECISION (discipline rule): " + verdict + "\n"
                 + "string confirmed: " + str({k: sorted(v) for k, v in scon.items()}) + "\n"
                 + "dtb confirmed: " + str({k: sorted(v) for k, v in dcon.items()}) + "\n"
                 + "elf confirmed: " + str({k: sorted(v) for k, v in econ.items()}) + "\n"
                 + "reasons: " + "; ".join(reasons) + "\n"
                 + "NOTE: memset/strncpy demotion in the STRING module is CORRECT (callers are out-of-corpus; real primitive bugs surface in elf).\n")
    return 0 if verdict == "PASS" else 1
sys.exit(main())
