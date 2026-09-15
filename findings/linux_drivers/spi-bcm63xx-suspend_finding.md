# spi: bcm63xx: return error from failed controller suspend

BMC-Agent discovered a suspend error-handling bug in the bcm63xx SPI driver.
Jiawen Liu submitted the fix, which was accepted into Linux mainline.

- Source: [`drivers/spi/spi-bcm63xx.c`](https://github.com/torvalds/linux/blob/bbbc1ef5b35341c5122418a14f8924688a4bb954/drivers/spi/spi-bcm63xx.c).
- Function: `bcm63xx_spi_suspend()`.
- Mainline commit: [`bbbc1ef5b353`](https://github.com/torvalds/linux/commit/bbbc1ef5b35341c5122418a14f8924688a4bb954).
- Submission: [patch email](https://patch.msgid.link/tencent_0BD7D4091B90EC17A8B2BA5EBA8803725905@qq.com).

## Bug

`bcm63xx_spi_suspend()` ignored the return value of
`spi_controller_suspend()`. If controller suspension failed, the driver
still disabled the controller clock and returned success.

## Fix

Check the return value and propagate the suspend error before disabling
the clock, preserving the controller's clock state on failure.
