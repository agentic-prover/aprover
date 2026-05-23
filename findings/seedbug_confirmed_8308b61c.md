# Seed bug confirmed: 8308b61c [ACL] Parser out-of-bounds read

**Status**: ✓ documented seed bug surfaced by bmc-agent-lite

## The bug

Upstream commit:
```
8308b61c [ACL] Parser out-of-bounds read
Author: Tim Kientzle <tkientzle@apple.com>
Date:   Thu May 7 19:41:04 2026 -0700

    The ACL parser fails to validate buffer length when processing PAX
    attributes (SCHILY.acl.access/default). The next_field() function
    attempts to read a separator character from a pointer even when the
    remaining length is zero.

    Reported-by: Kamil Frankowicz
```

Upstream fix (libarchive/archive_acl.c::next_field):
```c
-       *sep = **p;
+       if (*l > 0)
+               *sep = **p;
+       else
+               *sep = '\0';
```

Applied at BOTH dereferences in the function (the comment-handling block has the same OOB).

## What bmc-agent-lite found

Driver: `acl_validation/archive_acl`
Function: `next_field`
Property: `next_field.pointer_dereference.83`
Confidence: **confirmed_system_entry**
Call chain: `archive_acl_from_text_l → archive_acl_from_text_nl → next_field`
Realism verdict: `uncertain` (NOT downgraded to unrealistic)

Realism reasoning (excerpt):
> The violation is an out-of-bounds pointer dereference in `next_field`.
> The function takes a length parameter `*l` and uses it to bound
> iterations, but there's a critical issue: after the third while loop
> exits (when `*l == 0`), the code does `*sep = **p` unconditionally.
> If `*l` reached 0, `*p` points one past the end of the valid buffer,
> making `**p` an out-of-bounds read.

This is precisely the bug the commit fixed.

## Why bmc-agent-lite missed this before

The N=1 CEx-dedup (`_dedup_counterexamples` kept exactly one CEx per
property type) discarded the `pointer_dereference.83` CEx. The first
CEx of type `pointer_dereference` in `next_field` was an artifact-
flavoured one (typical iter-0 nondet-pointer pattern); realism
correctly rejected it; the deeper `.83` CEx that contains the real
bug never reached classification.

The N=3 widening (commit `b12ce08`, env `BMC_AGENT_DEDUP_MAX_PER_TYPE=3`)
kept the deeper index and let realism rule on it. Realism returned
`uncertain` (NOT unrealistic) because the witness is plausibly
reachable from real callers.

## Implication for the goal

Goal: **≥10 documented real bugs in libarchive, no noise**.

* Bug count so far: **1 confirmed**
* Sweep still in progress (archive_acl.c phase 3 ~50% complete, 6
  more files queued)
* The dedup widening is empirically necessary: this bug is *only*
  reachable through that fix. The baseline N=1 sweep recorded
  `next_field` as `verified=False, no phase-3 report` — completely
  invisible as a finding.

## Sweep configuration that surfaced the bug

```
verify-dir
  --source-dir /tmp/libarchive_acl_validation
  --driver acl_validation
  --output /tmp/libarchive_acl_validation_out
  --lite-mode
  --enable-realism-check
  --skip-refinement
  --include-dir .../libarchive/build
  --include-dir .../libarchive/libarchive
  -D HAVE_CONFIG_H
```
with `BMC_AGENT_DEDUP_MAX_PER_TYPE=3` (default after `b12ce08`).
