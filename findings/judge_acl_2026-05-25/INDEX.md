# Bug reports — BMC-confirmed adjacent findings

Generated from /tmp/libarchive_judge_acl

6 bugs total.

| # | Function | Bug type | Seed-bug map |
|---|---|---|---|
| [1](./01_isint__Undefined_behavior_relational_comparison.md) | `isint` | Undefined behavior: relational comparisons on possibly NULL  | Related to 4b3ba035 family (sibling helper that receives the |
| [2](./02_is_nfs4_flags__Undefined_behavior_on_missing_NFSv4_fiel.md) | `is_nfs4_flags` | Undefined behavior on missing NFSv4 fields (NULL start/end) | Related to 4b3ba035 family (sibling helper that receives the |
| [3](./03_archive_acl_from_text_nl__NULL_pointer_arithmetic_undefined_behavi.md) | `archive_acl_from_text_nl` | NULL pointer arithmetic/undefined behavior on truncated NFSv | MATCH documented seed 4b3ba035 (NULL deref in archive_acl_fr |
| [4](./04_next_field__out_of_bounds_read_potential_at_end_of_i.md) | `next_field` | out-of-bounds read potential at end-of-input (dereferencing  | MATCH documented seed 8308b61c (ACL parser OOB read) |
| [5](./05_isint_w__Potential_UB_relational_comparison_on_po.md) | `isint_w` | Potential UB: relational comparison on possibly NULL pointer | Related to 4b3ba035 family (sibling helper that receives the |
| [6](./06_is_nfs4_perms_w__Potential_UB_relational_loop_condition_o.md) | `is_nfs4_perms_w` | Potential UB: relational loop condition on possibly NULL poi | Related to 4b3ba035 family (sibling helper that receives the |