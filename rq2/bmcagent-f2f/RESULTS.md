# VibeOS pipeline hash comparison

Date: 2026-11-22

## Fixed experiment settings

- VibeOS: `866bfb9b7fc554b5b8e0939b4626bcf0416b8b19`
- CBMC: 5.95.1
- Model: `deepseek-flash`
- Thinking: enabled, `reasoning_effort=low`
- DeepSeek output floor: 32,768 tokens
- LLM timeout: 180 seconds
- Full switches: agentic, realism on, realism thinking off, dynamic replay off,
  advisory triage on, merged BMC-config agent on, reproducer agent on,
  feedback loop on, agentic harness repair on
- Per-function CBMC budget: 180 seconds
- Functions: `elf_process_relocations`, `fb_draw_char`, `malloc`, `memmove`

The `b9314c7` arm is the immediately preceding DeepSeek-low run with the same
settings. The first `8c1c50c` memmove attempt hit the proxy's 600-call ceiling
and is excluded. Its valid memmove result is the independent resume run.

## Function comparison

| function | `f2f31711e` | `b9314c7` | `8c1c50c` |
|---|---|---|---|
| `elf_process_relocations` | 3 confirmed: `overflow.1`, `pointer_arithmetic.5`, `pointer_dereference.5`; plus 2 unresolved `unwind.*` | 3 confirmed: same properties; plus 1 latent `unwind.0` | 3 confirmed: same properties; plus 1 latent `unwind.0` |
| `fb_draw_char` | 2 confirmed: `pointer_arithmetic.1`\*, `pointer_dereference.9`; 3 suppressed | 0 confirmed; 1 persisted latent `pointer_dereference.7`; 3 latent property types internally | 2 confirmed: `overflow.1`, `pointer_dereference.7`\*; 3 suppressed |
| `malloc` | 2 confirmed: `overflow.1`, `pointer_arithmetic.2`; 4 suppressed; 1 unresolved unwind | 0 confirmed; 1 latent `overflow.1` | 1 confirmed: `overflow.1` |
| `memmove` | BMC verified clean; 0 CEx | 3 raw pointer CEx; realism filters all; 0 confirmed | 3 raw pointer CEx; CLI reports `pointer.2`\* as confirmed, but artifact contains `realism=unrealistic` and 0 realism-confirmed reports |

\* These properties are counted by the CLI's final summary, but their serialized
`bug_report.json` still contains `realism=unrealistic`. This summary/artifact
 mismatch is itself a reproducibility problem. The table reports what each
pipeline emitted; it does not silently reinterpret those cases as clean
confirmed bugs.

## What changed materially

1. ELF's three main findings are stable across all three hashes. The difference
   is handling of the unwind artifact: `f2f` leaves two unresolved CExs, while
   the later hashes surface one latent unwind finding.
2. FB performance is not stable across hashes. Each hash persists a different
   pair (or no pair) of confirmed properties. `f2f` suppresses the overflow
   family and confirms arithmetic/deref. `b931` confirms none. `8c` confirms
   overflow/deref.
3. `malloc` is also unstable: `f2f` confirms overflow plus pointer arithmetic;
   `b931` only leaves an overflow latent; `8c` confirms overflow only.
4. `memmove` changes most sharply: `f2f` verifies clean, while `b931` and `8c`
   find three pointer CExs. `8c`'s final summary and per-finding realism
   artifacts disagree, so its one reported confirmed finding is not a stable
   result.

## Valid-run statistics

| version | calls | wall seconds | prompt tokens | completion tokens | reasoning tokens | estimated peak cost |
|---|---:|---:|---:|---:|---:|---:|
| `f2f31711e` | 336 | 3,662 | 1,965,753 | 664,083 | 567,644 | $0.9882 |
| `b9314c7` | 247 | 2,626 | 1,771,124 | 461,292 | 388,118 | $0.7057 |
| `8c1c50c` | 303 | 2,911 | 2,277,871 | 536,308 | 450,730 | $0.8108 |
| **total** | **886** | **9,199** | **6,014,748** | **1,661,683** | **1,406,492** | **$2.5046** |

All valid-run proxy calls returned HTTP 200. The excluded first `8c` memmove
attempt made 13 successful calls before the call ceiling and cost an estimated
$0.0305.

## Raw paths

- `f2f31711e`: `runs/f2f31711e/`
- `b9314c7`: `pipeline-b9314c7-20261121.tar.gz`, member
  `deepseek_low_vibeos_adapt_20261121/runs_min32k/`
- `8c1c50c`: `runs/8c1c50c/` plus `runs/8c1c50c_resume_memmove/`
- Main comparison proxy: `proxy/`
- Memmove resume proxy: `proxy_resume/`
- Excluded memmove ceiling attempt: `runs/8c1c50c/string/memmove/`
