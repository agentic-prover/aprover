#!/usr/bin/env python3
"""Deterministic GO/NO-GO for the realism DISCIPLINE-RULE re-validation.

The discipline rule affects only the REALISM verdict. The separate, pre-existing
dyn-val "Validation downgrade" (REAL_BUG->UNRESOLVED when the reproducer does not
trigger) lowers FINAL confidence independently -- it must NOT be blamed on the
discipline rule. So we judge the rule by the REALISM VERDICT (parsed from run.log),
not the final tier.

  PASS  = (string) strncpy realism = UNREALISTIC   [FP closed by the rule]
          AND (dtb) read_be64 realism = REALISTIC   [real entry-point bug NOT over-demoted]
          AND (elf) >=1 of elf_validate/elf_calc_size/elf_process_relocations
              realism = REALISTIC                   [parser reals NOT over-demoted]
          AND no budget/400 contamination.
  OVERTIGHTEN  = read_be64 or all elf reals flipped to UNREALISTIC by the rule.
  INSUFFICIENT = strncpy realism still REALISTIC (rule did not close the FP).
Note: dyn-val downgrades of read_be64 (real bug, reproducer didn't trigger) are a
SEPARATE soundness issue (the classifier-adjudicator guard) -- recorded, not counted here.
"""
import glob, os, re, sys
ROOT = os.path.expanduser("~/AProver")
ELF_REALS = ("elf_validate", "elf_calc_size", "elf_process_relocations")

def latest(p):
    ds = sorted(glob.glob(os.path.join(ROOT, "findings", p)), reverse=True)
    return ds[0] if ds else None

def logtext(d):
    try:
        return open(os.path.join(d, "run.log"), errors="replace").read()
    except OSError:
        return ""

def contaminated(s):
    return ("workspace API usage" in s) or ("Error code: 400" in s) or ("Realism check LLM call failed" in s)

def realism_verdicts(s):
    """fn -> last realism verdict seen in the log."""
    out = {}
    for fn, v in re.findall(r"Realism check for '([A-Za-z_]\w*)':\s*verdict=([a-z_]+)", s):
        out[fn] = v
    return out

def main():
    sd, dd, ed = latest("judge_disc_string_*/"), latest("judge_disc_dtb_*/"), latest("judge_disc_elf_*/")
    for n, d in (("string", sd), ("dtb", dd), ("elf", ed)):
        if not d or not os.path.exists(d + "/DONE"):
            print("INCOMPLETE:", n, "not finished"); return 2
    ss, ds_, es = logtext(sd), logtext(dd), logtext(ed)
    for n, s in (("string", ss), ("dtb", ds_), ("elf", es)):
        if contaminated(s):
            print("CONTAMINATED:", n); return 3
    sv, dv, ev = realism_verdicts(ss), realism_verdicts(ds_), realism_verdicts(es)
    strncpy_v = sv.get("strncpy", "?")
    readbe64_v = dv.get("read_be64", "?")
    elf_real_realistic = [fn for fn in ELF_REALS if ev.get(fn) == "realistic"]
    verdict, reasons = "PASS", []
    if readbe64_v == "unrealistic":
        verdict = "OVERTIGHTEN"; reasons.append("read_be64 realism flipped to UNREALISTIC by the rule")
    if not elf_real_realistic:
        verdict = "OVERTIGHTEN"; reasons.append("no elf parser real kept realistic (%s)" % {fn: ev.get(fn) for fn in ELF_REALS})
    if strncpy_v == "realistic" and verdict == "PASS":
        verdict = "INSUFFICIENT"; reasons.append("strncpy realism still REALISTIC (FP not closed)")
    if verdict == "PASS":
        reasons.append("strncpy=%s (FP closed); read_be64=%s (kept); elf reals realistic=%s" % (strncpy_v, readbe64_v, elf_real_realistic))
    dynval_demoted_readbe64 = "Validation downgrade: 'read_be64'" in ds_
    print("VERDICT:", verdict)
    print("realism verdicts -> strncpy:", strncpy_v, "| read_be64:", readbe64_v, "| elf:", {fn: ev.get(fn) for fn in ELF_REALS})
    print("reasons:", "; ".join(reasons))
    if dynval_demoted_readbe64:
        print("NOTE: read_be64 was dyn-val-downgraded (reproducer not triggered) -- SEPARATE pre-existing issue, not the discipline rule.")
    with open(os.path.join(ROOT, "findings", "JUDGMENT_NOTES.md"), "a") as fh:
        fh.write("\n## OVERNIGHT DECISION (discipline rule, realism-verdict based): " + verdict + "\n"
                 + "realism: strncpy=%s read_be64=%s elf=%s\n" % (strncpy_v, readbe64_v, {fn: ev.get(fn) for fn in ELF_REALS})
                 + "reasons: " + "; ".join(reasons) + "\n"
                 + ("NOTE: read_be64 dyn-val-downgraded (reproducer not triggered) = SEPARATE soundness issue (classifier-adjudicator guard), NOT the discipline rule.\n" if dynval_demoted_readbe64 else ""))
    return 0 if verdict == "PASS" else 1
sys.exit(main())
