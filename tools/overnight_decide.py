#!/usr/bin/env python3
"""Deterministic GO/NO-GO for the realism discipline-rule re-validation.

Reads the disc_string + disc_dtb findings and applies the pre-set criteria
(no LLM/judgment needed — the judgment was already made; this checks outcomes):
  PASS  = strncpy DEMOTED (FP closed) AND memset.pointer_arithmetic.1 KEPT
          (real bug, concrete elf_load_at caller) AND dtb read_be64 KEPT
          (real OOB via entry point) AND no budget/400 contamination.
  OVERTIGHTEN = a real bug (memset / read_be64) got demoted -> rule too strict.
  INSUFFICIENT = strncpy still confirmed -> rule did not close the FP.
  CONTAMINATED / INCOMPLETE = budget errors or runs not finished.
Exit 0 only on PASS. Writes a decision block to stdout + findings/JUDGMENT_NOTES.md.
"""
import json, glob, os, sys

ROOT = os.path.expanduser("~/AProver")

def latest(patt):
    ds = sorted(glob.glob(os.path.join(ROOT, "findings", patt)), reverse=True)
    return ds[0] if ds else None

def contaminated(d):
    try:
        s = open(os.path.join(d, "run.log"), errors="replace").read()
    except OSError:
        return True
    return ("workspace API usage" in s) or ("Error code: 400" in s) or ("Realism check LLM call failed" in s)

def confirmed_props(d):
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
    sd = latest("judge_disc_string_*/")
    dd = latest("judge_disc_dtb_*/")
    if not sd or not dd or not os.path.exists(sd + "/DONE") or not os.path.exists(dd + "/DONE"):
        print("INCOMPLETE: disc_string and/or disc_dtb not finished")
        return 2
    if contaminated(sd) or contaminated(dd):
        print("CONTAMINATED: budget/400/realism-call-failure in a disc run -> invalid")
        return 3
    scon = confirmed_props(sd)
    dcon = confirmed_props(dd)
    strncpy_confirmed = bool(scon.get("strncpy"))
    memset_kept = "memset.pointer_arithmetic.1" in scon.get("memset", set())
    readbe64_kept = any("read_be64" in p for p in dcon.get("read_be64", set()))
    verdict = "PASS"
    reasons = []
    if not memset_kept:
        verdict = "OVERTIGHTEN"; reasons.append("memset.pointer_arithmetic.1 (REAL, concrete caller) was DEMOTED")
    if not readbe64_kept:
        verdict = "OVERTIGHTEN"; reasons.append("dtb read_be64 (REAL OOB, entry point) was DEMOTED")
    if strncpy_confirmed and verdict == "PASS":
        verdict = "INSUFFICIENT"; reasons.append("strncpy still CONFIRMED (FP not closed)")
    if verdict == "PASS":
        reasons.append("strncpy demoted (FP closed); memset.pointer_arithmetic.1 kept; read_be64 kept")
    print("VERDICT:", verdict)
    print("string confirmed:", {k: sorted(v) for k, v in scon.items()})
    print("dtb confirmed:", {k: sorted(v) for k, v in dcon.items()})
    print("reasons:", "; ".join(reasons))
    note = (
        "\n## OVERNIGHT DECISION (discipline-rule re-validation): " + verdict + "\n"
        + "string confirmed: " + str({k: sorted(v) for k, v in scon.items()}) + "\n"
        + "dtb confirmed: " + str({k: sorted(v) for k, v in dcon.items()}) + "\n"
        + "reasons: " + "; ".join(reasons) + "\n"
    )
    with open(os.path.join(ROOT, "findings", "JUDGMENT_NOTES.md"), "a") as fh:
        fh.write(note)
    return 0 if verdict == "PASS" else 1

sys.exit(main())
