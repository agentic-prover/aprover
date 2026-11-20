#!/bin/bash
# At-a-glance status for the running GPT-5 sweep.
LOG=${1:-/tmp/libarchive_gpt5_full_out/sweep.log}
OUT=${2:-/tmp/libarchive_gpt5_full_out}
echo "=== sweep status @ $(date +%T) ==="
ps -ef | grep -E "bmc-agent verify-dir|bmc_agent.cli verify-dir" | grep -v grep | head -3 || echo "no bmc-agent verify-dir process running"

echo
echo "--- files processed ---"
ls $OUT/seedhunt_gpt5/ 2>/dev/null

echo
echo "--- LLM usage ---"
GPT_CALLS=$(grep -c "LLM usage (openai)" $LOG 2>/dev/null)
echo "GPT-5 calls so far: $GPT_CALLS"
TOTAL_COMP=$(grep -A1 "LLM usage (openai)" $LOG 2>/dev/null | grep -oE "completion_tokens=[0-9]+" | awk -F= '{s+=$2} END {print s}')
TOTAL_PROMPT=$(grep -oE "prompt_tokens=[0-9]+" $LOG 2>/dev/null | awk -F= '{s+=$2} END {print s}')
echo "  prompt tokens:     $TOTAL_PROMPT"
echo "  completion tokens: $TOTAL_COMP"
# GPT-5 OpenRouter pricing approx: input $1.25/1M, output $10/1M
COST=$(python3 -c "print(f'\${${TOTAL_PROMPT:-0}*1.25e-6 + ${TOTAL_COMP:-0}*10e-6:.4f}')" 2>/dev/null)
echo "  ~cost (est):       $COST"

echo
echo "--- bug findings ---"
TOTAL_BR=$(find $OUT -name "bug_report.json" 2>/dev/null | wc -l)
HISTORY=$(find $OUT -path "*/bug_reports/*.json" 2>/dev/null | wc -l)
CLASSIF=$(find $OUT -name "classification.json" 2>/dev/null | wc -l)
echo "bug_report.json files:    $TOTAL_BR"
echo "per-CEx history records:  $HISTORY"
echo "classifications:          $CLASSIF"

echo
echo "--- verdict tallies ---"
python3 - <<PY 2>/dev/null
import json, glob, collections
verdicts = collections.Counter()
confs = collections.Counter()
for f in glob.glob('$OUT/**/bug_report.json', recursive=True):
    try:
        d = json.load(open(f))['report']
    except: continue
    confs[d.get('confidence') or '(none)'] += 1
    rc = d.get('realism_check') or {}
    verdicts[rc.get('verdict') or '(none)'] += 1
print("confidence:", dict(confs))
print("verdict   :", dict(verdicts))
PY

echo
echo "--- recent phase activity ---"
grep -E "Phase [0-9]+[bc]?|=== Processing|Validating counter|REALISTIC|UNREALISTIC|REAL_BUG|SPURIOUS" $LOG 2>/dev/null | tail -8

echo
echo "--- errors / warnings ---"
grep -iE "ERROR|LLMError|Traceback" $LOG 2>/dev/null | grep -vE "CBMC error|parser_error|cbmc exited" | head -3
