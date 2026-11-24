# RQ2 component ablation

The ablation covers 47 functions from the VibeOS `elf`, `fb`, `memory`, and
`string` modules. These functions contain 11 findings from the original RQ2
reference set.

`table-vii.tsv` is the paper-ready result table. `coverage.tsv` records the
number of functions that completed all enabled CBMC checks, including the
`5/47` value discussed in the text. The remaining files contain the raw
counts, validity checks, per-configuration results, and the script used to
aggregate them.

The three ablations use:

- BMC-Agent `f2f31711e`;
- VibeOS `866bfb9`;
- CBMC 5.95.1;
- Claude Sonnet 4.6 through the Anthropic-native OpenRouter path;
- a 180-second per-function CBMC budget.

Email rerolls and cover letters are not separate ablation results. The source
archive contains the complete runs, proxy ledgers, generated artifacts, and
the executed scripts. The copies in `scripts/` are parameterised for use
outside the original host: set the documented environment variables rather
than relying on the recorded local paths.
