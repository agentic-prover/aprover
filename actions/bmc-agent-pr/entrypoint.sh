#!/usr/bin/env bash
# bmc-agent-pr entrypoint.
#
# Runs inside the action's Docker container. Detects C source files changed
# by the current PR, preprocesses each, runs bmc-agent verify, aggregates
# the per-function verdicts, and posts a PR comment summarising any
# real_bug findings.
#
# Required env vars (set by action.yml):
#   ANTHROPIC_API_KEY        — LLM credential
#   BMC_AGENT_LLM_BASE_URL   — LLM endpoint
#   BMC_AGENT_LLM_MODEL      — model id
#   INPUT_SOURCE_GLOBS       — space-separated globs of files to analyse
#   INPUT_CFLAGS             — extra cflags forwarded to gcc -E
#   INPUT_THREAT_MODEL       — security|safety|functional
#   INPUT_FEATURE_FLAGS      — extra bmc-agent CLI flags
#   INPUT_MAX_FUNCTIONS      — safety cap per PR
#   INPUT_FAIL_ON_REAL_BUG   — bool; if true the action exits non-zero on real_bug
#   GITHUB_TOKEN             — for posting PR comments
#   GITHUB_EVENT_PATH        — set by GitHub Actions
#   GITHUB_WORKSPACE         — set by GitHub Actions

set -euo pipefail
# Enable recursive ** glob and extended glob features used by the
# source-glob matcher below.
shopt -s globstar extglob nullglob

log() { printf '::group::%s\n' "$*"; }
endgroup() { printf '::endgroup::\n'; }
warn() { printf '::warning::%s\n' "$*"; }
err()  { printf '::error::%s\n' "$*"; }

WORKSPACE="${GITHUB_WORKSPACE:-/github/workspace}"
cd "${WORKSPACE}"

# GitHub Actions mounts the workspace from the host; the container user
# differs from the directory owner, which trips git's dubious-ownership
# check. Mark the workspace safe — the directory is under our control
# inside the action run.
git config --global --add safe.directory "${WORKSPACE}"

# ---------------------------------------------------------------------------
# Determine the base ref to diff against. On pull_request events GitHub
# provides github.event.pull_request.base.sha; on push events we diff
# against the previous commit.
# ---------------------------------------------------------------------------

if [[ -n "${GITHUB_EVENT_PATH:-}" && -f "${GITHUB_EVENT_PATH}" ]]; then
    BASE_SHA="$(jq -r '.pull_request.base.sha // .before // empty' "${GITHUB_EVENT_PATH}")"
else
    BASE_SHA=""
fi
HEAD_SHA="$(git rev-parse HEAD)"

if [[ -z "${BASE_SHA}" || "${BASE_SHA}" == "null" ]]; then
    warn "Could not determine base SHA; falling back to HEAD~1"
    BASE_SHA="$(git rev-parse HEAD~1 2>/dev/null || echo "")"
fi

if [[ -z "${BASE_SHA}" ]]; then
    err "Could not determine a base SHA to diff against. Aborting."
    exit 1
fi

log "Diff range: ${BASE_SHA}..${HEAD_SHA}"
git --no-pager log --oneline "${BASE_SHA}..${HEAD_SHA}" || true
endgroup

# ---------------------------------------------------------------------------
# Collect changed C source files matching the user-supplied globs.
# ---------------------------------------------------------------------------

log "Identifying changed source files"
mapfile -t CHANGED_FILES < <(
    git diff --name-only --diff-filter=AM "${BASE_SHA}..${HEAD_SHA}" \
        | grep -E '\.(c|h)$' || true
)

# Filter to user-supplied globs. Default globs cover both top-level and
# subdirectory .c/.h files (the ``**/`` form requires a slash, so it does
# not match top-level files on its own — include the bare extension too).
SOURCE_GLOBS="${INPUT_SOURCE_GLOBS:-**/*.c *.c **/*.h *.h}"
SELECTED=()
for f in "${CHANGED_FILES[@]:-}"; do
    [[ -z "${f}" ]] && continue
    for pattern in ${SOURCE_GLOBS}; do
        # bash globbing on a literal filename via [[ == ]]
        if [[ "${f}" == ${pattern} ]]; then
            SELECTED+=("${f}")
            break
        fi
    done
done

if [[ "${#SELECTED[@]}" -eq 0 ]]; then
    echo "No C source files changed in this PR. Nothing to do."
    endgroup
    exit 0
fi

printf '  selected: %s\n' "${SELECTED[@]}"
endgroup

# ---------------------------------------------------------------------------
# Preprocess each changed file with gcc -E. The user is expected to pass
# project-specific include paths and defines via INPUT_CFLAGS. For projects
# already on OSS-Fuzz, the recommended approach is to invoke the project's
# Docker image to preprocess (see README) rather than rely on raw gcc -E.
# ---------------------------------------------------------------------------

PREP_DIR="$(mktemp -d)"
ARTIFACT_DIR="$(mktemp -d)"
FINDINGS_JSON="${ARTIFACT_DIR}/findings.json"
echo '[]' > "${FINDINGS_JSON}"

# shellcheck disable=SC2086
CFLAGS_ARRAY=(${INPUT_CFLAGS:-})

MAX_FUNCTIONS="${INPUT_MAX_FUNCTIONS:-25}"
ANALYZED=0
REAL_BUG_COUNT=0

for src in "${SELECTED[@]}"; do
    [[ "${ANALYZED}" -ge "${MAX_FUNCTIONS}" ]] && {
        warn "Reached max-functions cap (${MAX_FUNCTIONS}); skipping remaining files."
        break
    }

    base="$(basename "${src}" .c)"
    base="${base%.h}"
    ipath="${PREP_DIR}/${base}.i"
    log "Preprocessing ${src}"
    if ! gcc -E -P "${CFLAGS_ARRAY[@]+"${CFLAGS_ARRAY[@]}"}" "${src}" -o "${ipath}" 2>&1; then
        warn "Preprocessing failed for ${src}; skipping."
        endgroup
        continue
    fi
    endgroup

    log "Running bmc-agent verify on ${src}"
    driver="${base//[^a-zA-Z0-9_]/_}"
    out="${ARTIFACT_DIR}/${driver}"
    set +e
    # shellcheck disable=SC2086
    bmc-agent verify \
        --source "${ipath}" \
        --driver "${driver}" \
        --output "${out}" \
        --threat-model "${INPUT_THREAT_MODEL:-security}" \
        ${INPUT_FEATURE_FLAGS:-} \
        > "${ARTIFACT_DIR}/${driver}.log" 2>&1
    rc=$?
    set -e
    if [[ "${rc}" -ne 0 ]]; then
        warn "bmc-agent verify exited with status ${rc} on ${src}; see log."
        tail -n 40 "${ARTIFACT_DIR}/${driver}.log" || true
    fi
    endgroup

    # Aggregate any classification.json files for this file into findings.json.
    while IFS= read -r cls_path; do
        outcome="$(jq -r '.classification.outcome // ""' "${cls_path}" 2>/dev/null)"
        [[ -z "${outcome}" || "${outcome}" == "null" ]] && continue
        fn_dir="$(dirname "${cls_path}")"
        fn_name="$(basename "${fn_dir}")"
        prop="$(jq -r '.classification.counterexample.failing_property // ""' "${cls_path}")"
        reason="$(jq -r '.classification.reasoning // ""' "${cls_path}" | head -c 500)"
        jq --arg src "${src}" \
           --arg fn "${fn_name}" \
           --arg outcome "${outcome}" \
           --arg prop "${prop}" \
           --arg reason "${reason}" \
           '. += [{source:$src, function:$fn, outcome:$outcome, failing_property:$prop, reasoning:$reason}]' \
           "${FINDINGS_JSON}" > "${FINDINGS_JSON}.new"
        mv "${FINDINGS_JSON}.new" "${FINDINGS_JSON}"
        if [[ "${outcome}" == "real_bug" ]]; then
            REAL_BUG_COUNT=$((REAL_BUG_COUNT + 1))
        fi
    done < <(find "${out}" -name classification.json 2>/dev/null || true)

    ANALYZED=$((ANALYZED + 1))
done

# ---------------------------------------------------------------------------
# Format a PR comment from the aggregated findings and post via gh.
# ---------------------------------------------------------------------------

log "Findings summary"
jq -r '.[] | "  [\(.outcome | ascii_upcase)] \(.source)::\(.function)\n    prop: \(.failing_property)\n    why : \(.reasoning | gsub("\\n"; " ") | .[0:200])"' \
   "${FINDINGS_JSON}" || true
echo "real_bug count: ${REAL_BUG_COUNT}"
endgroup

PR_NUMBER="$(jq -r '.pull_request.number // empty' "${GITHUB_EVENT_PATH:-/dev/null}" 2>/dev/null || true)"

if [[ -n "${PR_NUMBER}" && -n "${GITHUB_TOKEN:-}" ]]; then
    log "Posting PR comment"
    {
        echo "## bmc-agent results"
        echo
        if [[ "${REAL_BUG_COUNT}" -eq 0 ]]; then
            echo "No \`real_bug\` verdicts across ${ANALYZED} changed file(s)."
        else
            echo "**${REAL_BUG_COUNT}** \`real_bug\` verdict(s) on ${ANALYZED} changed file(s):"
            echo
            jq -r '.[] | select(.outcome == "real_bug") |
                "- **\(.source)::\(.function)** — \(.failing_property)\n  > \(.reasoning | gsub("\\n"; " ") | .[0:300])"' \
                "${FINDINGS_JSON}"
        fi
        echo
        echo "<details><summary>All verdicts (\($(jq 'length' "${FINDINGS_JSON}")))</summary>"
        echo
        echo '```json'
        jq '.' "${FINDINGS_JSON}"
        echo '```'
        echo "</details>"
    } > "${ARTIFACT_DIR}/comment.md"

    GH_TOKEN="${GITHUB_TOKEN}" gh pr comment "${PR_NUMBER}" \
        --body-file "${ARTIFACT_DIR}/comment.md" \
        || warn "Failed to post PR comment (continuing)."
    endgroup
else
    warn "Skipping PR comment (no PR number or GITHUB_TOKEN)."
fi

# Surface outputs back to subsequent steps in the user's workflow.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "findings-json=${FINDINGS_JSON}" >> "${GITHUB_OUTPUT}"
    echo "real-bug-count=${REAL_BUG_COUNT}" >> "${GITHUB_OUTPUT}"
fi

if [[ "${INPUT_FAIL_ON_REAL_BUG:-false}" == "true" && "${REAL_BUG_COUNT}" -gt 0 ]]; then
    err "bmc-agent reported ${REAL_BUG_COUNT} real_bug verdict(s) and fail-on-real-bug is enabled."
    exit 1
fi

exit 0
