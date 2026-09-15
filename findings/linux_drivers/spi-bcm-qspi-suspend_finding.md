# spi: bcm-qspi: return error from failed controller suspend

BMC-Agent discovered a suspend error-handling bug in the bcm-qspi driver.
Jiawen Liu submitted the fix, which was accepted into Linux mainline.

- Source: [`drivers/spi/spi-bcm-qspi.c`](https://github.com/torvalds/linux/blob/44d09ef0ba690ac46d17eb05c12a47a5dd75e231/drivers/spi/spi-bcm-qspi.c).
- Function: `bcm_qspi_suspend()`.
- Mainline commit: [`44d09ef0ba69`](https://github.com/torvalds/linux/commit/44d09ef0ba690ac46d17eb05c12a47a5dd75e231).
- Submission: [patch email](https://patch.msgid.link/tencent_21BF5F9512F56D45FD9018BAF14ED2805808@qq.com).

## Bug

`bcm_qspi_suspend()` did not check whether `spi_controller_suspend()`
succeeded. A failed controller suspend was followed by clock and hardware
teardown, and the callback returned success.

## Fix

Check and return the controller suspend error before proceeding with
hardware teardown.
