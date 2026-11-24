# Frozen RQ2 BMC-Agent pipeline

The RQ2 results use BMC-Agent commit:

`f2f31711e5d0317ff21d16fd03cd912b152ea202`

This directory preserves:

- `source-f2f31711e.tar.gz`: the complete tracked source snapshot at that
  commit;
- `../source-vibeos-866bfb9.tar.gz`: the evaluated VibeOS source snapshot;
- `versions.tsv`: the exact BMC-Agent and VibeOS revisions;
- `pipeline-compare-20261121.tar.gz`: the `f2f31711e` and `8c1c50c`
  comparison runs and logs;
- `pipeline-b9314c7-20261121.tar.gz`: the successful `b9314c7`
  representative run, proxy ledger, and runner;
- `README.md` and `RESULTS.md`: the experiment scope and captured results.

The comparison archives contain all three source revisions reported in
`RESULTS.md`. Generated caches are excluded.
