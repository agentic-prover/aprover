# RQ3. Specification quality

| Evaluation | Inputs and results |
| --- | --- |
| [C/ACSL](acsl/README.md) | AutoSpec's 251 C programs, with five trials per case checked using Frama-C/WP. |
| [Java/JML](jml/README.md) | SpecGenBench and the SV-COMP Java subset, with ten trials per case and model checked using OpenJML. |

The [proof bundle](accepted-proofs-20261104.tar.gz) includes the accepted ACSL
and Claude JML inputs, verifier settings, and file hashes. It contains 378
distinct ACSL inputs and 932 distinct JML input bundles. The
[validation summary](accepted-proofs-validation.json) records replay coverage.
Follow the
[reproduction guide](../bmcagent/experiments/paper_reproduction/README.md#replay-accepted-proofs)
to replay the checks.
