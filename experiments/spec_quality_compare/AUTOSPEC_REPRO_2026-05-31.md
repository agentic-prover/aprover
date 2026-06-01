# AutoSpec Reproduction Smoke

Date: 2026-05-31

## Scope

This is a smallest useful AutoSpec reproduction probe, not a full 251-program
run. The goal is to confirm that the official artifact, Frama-C/WP toolchain,
AutoSpec decomposition frontend, and AutoSpec LLM loop can run on this server.

## Artifact

- Source artifact: Zenodo record `10912658`, `AutoSpec.zip`
- Local zip: `/mnt/disk7/jw_bmc/spec_quality_data/AutoSpec.zip`
- Extracted runnable copy: `/mnt/disk7/jw_bmc/spec_quality_data/autospec_artifact/AutoSpec`
- Extracted copy excludes the artifact's original `conf/config.json`.
- Local config uses OpenRouter-compatible API URL but does not store an API key.

## Toolchain

- Docker context: `default`, `/var/run/docker.sock`
- Frama-C image: `framac/frama-c:26.0.debian`
- Frama-C version: `26.0 (Iron)`
- Why3 version: `1.5.1`
- Z3 in image: `4.8.10`
- AutoSpec requested Z3 in README: `4.8.6`
- AutoSpec bundled clang/LLVM: `12.0.1`
- AutoSpec Python env: `/mnt/disk7/jw_bmc/spec_quality_data/autospec_env`
- OpenAI SDK: `openai==0.28.1`

## Safety Adapters

- `src/parse_args.py` in the extracted working copy was changed only to print
  `OPENAI_API_KEY = <redacted>`.
- `conf/config.json` in the extracted working copy has no key field.
- `frama-c` is provided through
  `/mnt/disk7/jw_bmc/spec_quality_data/autospec_wrappers/frama-c`, which runs
  the pinned Docker image.

## Results

| Probe | Command shape | Result |
|---|---|---|
| Official verified scalar case | `frama-c -wp -wp-precond-weakening -wp-no-callee-precond -wp-prover Alt-Ergo,Z3 ... max_of_2_verified.c` | `6 / 6` goals proved |
| Official verified loop case | same command on `fib_46_benchmark_verified/01_final_simplified.c` | `7 / 7` goals proved |
| Simplified scalar counterpart | same command on `frama-c-problems_verified/max_of_2_final_simplified.c` | rejected by Frama-C annotation parser: `requires` appears after `ensures` |
| AutoSpec decomposition | `python3 mark.py -f benchmark/fib_46_benchmark/01.c` | produced `01_marked.c`, `01_infilled.c`, and `.pickle` |
| AutoSpec LLM loop | `python3 fuzz.py -f benchmark/fib_46_benchmark/01.c -m gpt-3.5-turbo` | `final_result: Pass`; generated loop invariants and assigns |

Generated AutoSpec output:

- `/mnt/disk7/jw_bmc/spec_quality_data/autospec_artifact/AutoSpec/out/01_0001/01_merged.c`
- `/mnt/disk7/jw_bmc/spec_quality_data/autospec_artifact/AutoSpec/out/01_0001/final_result`
- `/mnt/disk7/jw_bmc/spec_quality_data/autospec_artifact/AutoSpec/out/01_0001/01_merged_Pass_8_8.txt`
- `/mnt/disk7/jw_bmc/spec_quality_data/autospec_artifact/AutoSpec/out/01_0001/01_merged_Pass_9_9.txt`

The generated loop contract for `fib_46_benchmark/01.c` was:

```c
/*@
 loop invariant x == y;
 loop invariant 1 <= y;
 loop invariant 1 <= x;
 loop assigns y;
 loop assigns x;
 */
```

## Current Interpretation

AutoSpec is reproducible enough to proceed with a small benchmark pilot:
the official artifact is present, Frama-C/WP runs under the pinned 26.0 image,
the bundled decomposition tool works, and one official LLM run reaches `Pass`.

Do not claim full paper replication yet. The scalar `*_final_simplified.c`
annotation-order rejection shows that some artifact outputs need case-by-case
classification before scaling.
