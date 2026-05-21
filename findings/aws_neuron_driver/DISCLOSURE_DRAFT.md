# Private disclosure draft — heap OOB read in ncdev_bar_read

**DO NOT SEND WITHOUT USER REVIEW AND APPROVAL.** This is a draft
for the user (theyoucheng / daniel1988xyz@gmail.com) to evaluate
after verifying the bug with a KASAN reproducer.

**Recipient:** security@aws.amazon.com (AWS Security)
**CC:** aws-neuron-sdk maintainers (via aws-neuron-driver repo
       security policy)
**Subject:** Heap out-of-bounds read in aws-neuron-driver
            ncdev_bar_read via NEURON_IOCTL_BAR_READ

## Pre-send checklist

- [ ] KASAN-instrumented kernel built with aws-neuron-driver loaded
- [ ] PoC code in `ncdev_bar_read_poc.c` compiled and run
- [ ] KASAN slab-out-of-bounds report obtained
- [ ] dmesg shows the report in `ncdev_bar_read+0x...`
- [ ] Reproducer attached to the email
- [ ] aws-neuron-driver commit hash recorded
- [ ] Affected kernel range (or "all") determined

## Draft body

---

Hello AWS Security,

I am writing to privately disclose a heap out-of-bounds read in
the aws-neuron-driver Linux kernel driver, discovered via static
analysis (bounded model checking) and verified with a KASAN
reproducer.

**Affected component:** aws-neuron-driver
**Affected commit:** <fill in>
**CWE:** CWE-125 (Out-of-bounds Read)

### Summary

`ncdev_bar_read` (neuron_cdev.c:1478) and its caller `ncdev_bar_rw`
(neuron_cdev.c:1622) have a length mismatch on the `reg_addresses`
array. For NEURON_IOCTL_BAR_READ with `arg.bar = 2`, the caller
allocates a 1-element `reg_addresses` buffer (8 bytes), but passes
the user-controlled `arg.count` to `ncdev_bar_read`, which loops
`for (i = 0; i < data_count; i++) reg_addresses[i]` and reads
arbitrary kernel heap memory beyond the 8-byte allocation.

### Reproducer

See attached `ncdev_bar_read_poc.c`. On a KASAN-instrumented
kernel with /dev/neuron0 accessible:

```
$ gcc ncdev_bar_read_poc.c -o poc
$ ./poc
# dmesg shows:
# BUG: KASAN: slab-out-of-bounds in ncdev_bar_read+0x.../...
# Read of size 8 at addr ffff... by task poc/...
```

### Impact

Local kernel heap out-of-bounds read. Triggerable by any process
with `/dev/neuronN` open access (root or `neuron` group depending
on deployment). The OOB-read values are validated against bar0
address range; if they happen to fall in that range, the function
proceeds to `fw_io_read_csr_array((void **)reg_addresses, ...)`
which dereferences OOB-derived addresses as MMIO register
pointers. Information disclosure and potentially MMIO state
change.

### Suggested fix

The caller's `address_count = 1` when `arg.bar != 0` reflects the
intent that BAR2 reads are sequential and only one base address is
needed. The callee's validation loop contradicts this. The minimum
fix is to either:

1. Reject `arg.count > 1` when `arg.bar != 0` at the IOCTL entry,
2. Pass `address_count` (not `arg.count`) to the loop, and
   autogenerate the rest of the addresses sequentially from
   `reg_addresses[0]`, OR
3. Allocate `arg.count` elements always.

Suggested patch:

```c
--- a/neuron_cdev.c
+++ b/neuron_cdev.c
@@ -1635,6 +1635,11 @@ static long ncdev_bar_rw(...)
     if (arg.bar == 0)
         address_count = arg.count;
     else
+    {
         address_count = 1;
+        if (arg.count != 1) {
+            return -EINVAL;
+        }
+    }

     reg_addresses = kmalloc(...);
```

### Discovery methodology

Discovered using AProver / bmc-agent (CBMC-based agentic bounded
model checker). bmc-agent flagged the function with
`ncdev_bar_read.pointer_dereference.11` CBMC verdict. Manual
inspection of the source confirmed the call-chain mismatch
between caller's `address_count = 1` and callee's loop over
`arg.count`.

### Disclosure timeline

I'd like to follow standard 90-day disclosure with private
reporting first. Please let me know:
- Whether AWS treats this as a security issue
- A CVE assignment if applicable
- A patched release timeline
- Whether the Neuron driver participates in coordinated
  disclosure programs

Thank you,
theyoucheng
daniel1988xyz@gmail.com

---

## Disclosure-channel notes

aws-neuron-driver does not have a published SECURITY.md.
Available channels:

1. **security@aws.amazon.com** — primary AWS Security email.
   Standard disclosure address for AWS products.
2. **GitHub Security Advisory** on `aws-neuron/aws-neuron-driver`
   — file a private GHSA at
   https://github.com/aws-neuron/aws-neuron-driver/security/advisories/new
   (requires GitHub account, but routes to maintainers privately).
3. **AWS Vulnerability Reporting Page** —
   https://aws.amazon.com/security/vulnerability-reporting/

Recommend channel #1 (security@aws.amazon.com) with channel #2 as
secondary so maintainers see it directly.
