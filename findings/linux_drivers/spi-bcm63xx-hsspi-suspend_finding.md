# spi: bcm63xx-hsspi: return error from failed controller suspend

BMC-Agent discovered a suspend error-handling bug in the bcm63xx HSSPI driver.
Jiawen Liu submitted the fix, which was accepted into Linux mainline.

- Source: [`drivers/spi/spi-bcm63xx-hsspi.c`](https://github.com/torvalds/linux/blob/3b5414d76a0381db77b9694537254af7acfa187a/drivers/spi/spi-bcm63xx-hsspi.c).
- Function: `bcm63xx_hsspi_suspend()`.
- Mainline commit: [`3b5414d76a03`](https://github.com/torvalds/linux/commit/3b5414d76a0381db77b9694537254af7acfa187a).
- Submission: [patch email](https://patch.msgid.link/tencent_B5A06807924A77C8690730EBF7A052AABE05@qq.com).

## Bug

`bcm63xx_hsspi_suspend()` ignored a failure from
`spi_controller_suspend()` and continued to disable the PLL and controller
clocks. It then reported a successful suspend despite the controller error.

## Fix

Return the controller suspend error before disabling either clock.
