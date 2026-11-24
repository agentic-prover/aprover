# Toolchain and Provenance

Prepared at: 2026-06-23T10:50:46.602691+00:00

## BMC-Agent Code

- Checkout: `${APROVER_ROOT}`
- Commit: `1528d0d Improve ACSL specs-bench helper handling`
- Full commit: `1528d0dcd9faecbc5b3fd40dc203aec38453af89`

## Dataset

- AutoSpec artifact benchmark root: `${AUTOSPEC_BENCHMARK_ROOT}`
- Included primary benchmark: 251 original C programs across Frama-C-problems, OOPSLA-13/FIB, SV-COMP subset, and SyGuS/Code2Inv.

## Verifier / Toolchain

- Frama-C/WP: `framac/frama-c:26.0.debian` through local wrapper
- CBMC: `6.9.0`
- BMC-Agent model route: OpenRouter-compatible OpenAI endpoint, model `anthropic/claude-sonnet-4.6`

## Secret Hygiene

No API key is stored in this artifact directory. Reproduction scripts require the caller to export `BMC_AGENT_LLM_API_KEY` before running.
