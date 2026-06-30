# bmc-agent-pr

A GitHub Action that runs [bmc-agent](https://github.com/anonymous/aprover)
on C source files changed by a pull request and posts a comment summarising
any `real_bug` verdicts. Inspired by Google
[CIFuzz](https://google.github.io/oss-fuzz/getting-started/continuous-integration/),
but uses LLM-driven specs + CBMC bounded model checking instead of fuzzing.

## What it does

1. Detects `*.c` / `*.h` files changed by the PR.
2. Preprocesses each file with `gcc -E` and the project-supplied cflags.
3. Runs `bmc-agent verify` with the full pipeline enabled (realism check,
   dynamic validation, feedback loop, per-function flag selection).
4. Aggregates per-function verdicts and posts a PR comment.
5. Optionally fails the build if any `real_bug` verdict was reported.

## Quick start

Drop this workflow into `.github/workflows/bmc-agent.yml`:

```yaml
name: bmc-agent

on:
  pull_request:
    paths:
      - '**/*.c'
      - '**/*.h'

jobs:
  bmc-agent:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # action needs full history to diff base..head

      - uses: anonymous/bmc-agent-pr@main
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          # Project-specific include paths and defines. Match what your build
          # system passes to the compiler.
          cflags: '-I./include -DHAVE_CONFIG_H'
          # Stop the build on a real_bug verdict (default: false).
          fail-on-real-bug: 'false'
```

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `anthropic-api-key` | yes | — | LLM credential. Pass via `secrets`. |
| `github-token` | no | `''` | Token for posting PR comments via `gh`. Pass `${{ secrets.GITHUB_TOKEN }}` from the workflow. |
| `llm-base-url` | no | `https://api.anthropic.com` | LLM endpoint. Override for OpenRouter / Azure / self-hosted. |
| `llm-model` | no | `claude-sonnet-4-6` | Model id. |
| `source-globs` | no | `**/*.c **/*.h` | Which changed files to analyse. |
| `cflags` | no | `''` | Extra cflags for `gcc -E`. **Set this for most real projects.** |
| `threat-model` | no | `security` | `security` \| `safety` \| `functional`. |
| `feature-flags` | no | all pipeline stages | bmc-agent CLI flags. |
| `max-functions` | no | `25` | Cap on files analysed per PR (LLM budget). |
| `fail-on-real-bug` | no | `false` | Exit non-zero on `real_bug`. |

## Outputs

| Output | Description |
|---|---|
| `findings-json` | Path to the aggregated per-function verdict JSON. |
| `real-bug-count` | Count of `real_bug` verdicts. |

## Using with OSS-Fuzz-covered projects

For projects already in OSS-Fuzz, the recommended preprocessing path is to
invoke the project's OSS-Fuzz build image rather than `gcc -E` directly —
that image already knows how to build the project with the right cflags.
Two patterns:

1. **Pre-build step in the workflow.** Before invoking the action, run the
   project's OSS-Fuzz Docker image to preprocess changed files into `.i`
   files, then point the action at those.
2. **Custom Dockerfile.** Fork this action and base its Dockerfile on
   `gcr.io/oss-fuzz/<project>` so all the project deps are pre-installed.
   The entrypoint then knows how to call `make CC='gcc -E ...'` or
   equivalent.

See [`example-workflow-ossfuzz.yml`](./example-workflow-ossfuzz.yml) for
the first pattern.

## Caveats

- **Preprocessing is the hard part.** For projects without a trivial build,
  `gcc -E` won't find the right headers. You must pass `cflags` that match
  the project's build configuration.
- **LLM cost.** Each function costs ~$0.01–0.05 in OpenRouter Sonnet
  pricing. A 25-function cap on PRs is the default; raise with care.
- **No ground-truth in CI.** A `real_bug` verdict is bmc-agent's best
  effort, not a proof. Triage every comment.
- **Action runtime.** A PR touching 10 .c files can take 15–30 minutes of
  wall-clock time. Configure timeouts appropriately.

## Output format

The PR comment looks like:

```
## bmc-agent results

**1** `real_bug` verdict(s) on 4 changed file(s):

- **src/foo.c::foo_handler** — foo_handler.pointer_arithmetic.5
  > 'foo_handler' is an entry function (no callers in any file).
  > The counterexample is directly reachable from the system boundary.

<details><summary>All verdicts (4)</summary>
...
</details>
```

## Building the image locally

For testing or self-hosted deployment, build the image yourself:

```bash
cd actions/bmc-agent-pr/
./prepare-build.sh    # copies bmc-agent source into ./bundle/
docker build -t bmc-agent-pr:test .
```

The Dockerfile prefers `./bundle/` if populated (private-repo case);
otherwise it falls back to cloning `${BMC_AGENT_REPO}#${BMC_AGENT_REF}`
(public-repo case, future).

## Smoke-test

To exercise the action end-to-end without GitHub Actions infrastructure:

```bash
# Set up a tiny "PR" — a git repo with a buggy commit on top.
SMOKE=/tmp/bmc-smoke; rm -rf $SMOKE && mkdir -p $SMOKE && cd $SMOKE
git init -q && git config user.email t@t && git config user.name t
echo 'int dbl(int x){ if(x>1000) return 0; return x*2; }' > foo.c
git add foo.c && git commit -qm base
BASE=$(git rev-parse HEAD)
echo 'int dbl(int x){ return x*2; }' > foo.c
git commit -qam pr
echo "{\"pull_request\":{\"number\":1,\"base\":{\"sha\":\"$BASE\"}}}" > /tmp/event.json

# Run the action's container against the workspace.
docker run --rm \
    -v $SMOKE:/github/workspace \
    -v /tmp/event.json:/event.json:ro \
    -e GITHUB_WORKSPACE=/github/workspace \
    -e GITHUB_EVENT_PATH=/event.json \
    -e ANTHROPIC_API_KEY="$YOUR_KEY" \
    -e INPUT_SOURCE_GLOBS="**/*.c *.c" \
    -e INPUT_THREAT_MODEL=security \
    -e INPUT_FEATURE_FLAGS="--enable-realism-check --enable-flag-selection" \
    -e INPUT_MAX_FUNCTIONS=3 \
    bmc-agent-pr:test
```

On the example above, bmc-agent reports
``safe_double.overflow.1 — signed integer overflow on x * 2`` as a
REAL_BUG.

## Status

Experimental. Image build + smoke-test confirmed working locally on
2026-05-18. Not yet exercised in a live GitHub Actions runner.
Tracks bmc-agent's `main` branch by default; pin a commit by
building the action's Docker image yourself.
