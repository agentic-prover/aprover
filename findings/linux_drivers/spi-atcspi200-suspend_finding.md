# spi: atcspi200: return error from failed controller suspend

BMC-Agent discovered a suspend error-handling bug in the atcspi200 driver.
Jiawen Liu submitted the fix, which was accepted into Linux mainline.

- Source: [`drivers/spi/spi-atcspi200.c`](https://github.com/torvalds/linux/blob/f4b14e67baffd2c879765275bda998a183594e8b/drivers/spi/spi-atcspi200.c).
- Function: `atcspi_suspend()`.
- Mainline commit: [`f4b14e67baff`](https://github.com/torvalds/linux/commit/f4b14e67baffd2c879765275bda998a183594e8b).
- Submission: [patch email](https://patch.msgid.link/tencent_306FA547FD68D10EE4B2AE9C132060F12F06@qq.com).

## Bug

`atcspi_suspend()` ignored the result of `spi_controller_suspend()`.
When that call failed, the driver still disabled its clock and returned
success instead of preserving the active controller state.

## Fix

Propagate the controller suspend error and return before disabling the clock.
