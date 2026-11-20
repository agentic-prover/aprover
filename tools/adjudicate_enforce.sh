#!/usr/bin/env bash
# Adjudicate a Phase-3 enforcement run.log for the safety gate.
# Prints, per finding: function, final confidence tier, realism verdict, and
# whether enforcement re-tiered it. Usage: bash tools/adjudicate_enforce.sh <run.log>
set -u
f="${1:?usage: adjudicate_enforce.sh <run.log>}"
echo "===================== $f ====================="
echo "--- run finished? ---"
grep -E "^DONE |AMC Bug Report Summary" "$f" | tail -3
echo
echo "--- realism verdicts (upheld / downgraded) ---"
grep -iE "Realism (upheld|downgrad)|immunity ENFORCED-OFF|Confidence downgraded|REALISTIC|UNREALISTIC" "$f" \
  | sed -E 's/^[[:space:]]+//' | grep -ivE "^\[|INFO *$|^[0-9]" | sort -u | head -60
echo
echo "--- final report: Function -> Confidence ---"
grep -E "Function:|Confidence:" "$f" | sed -E 's/^[[:space:]]+//' | tail -80
echo
echo "--- enforcement re-tier log lines ---"
grep -iE "immunity ENFORCED-OFF|re-tier|Confidence downgraded to 'unlikely'" "$f" | sed -E 's/^[[:space:]]+//' | sort -u
