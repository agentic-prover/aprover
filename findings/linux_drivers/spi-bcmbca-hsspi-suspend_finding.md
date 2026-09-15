# spi: bcmbca-hsspi: return error from failed controller suspend

BMC-Agent discovered a suspend error-handling bug in the bcmbca HSSPI driver.
Jiawen Liu submitted the fix, which was accepted into Linux mainline.

- Source: [`drivers/spi/spi-bcmbca-hsspi.c`](https://github.com/torvalds/linux/blob/faa878d4805ca375738294bc52eae310eb287b16/drivers/spi/spi-bcmbca-hsspi.c).
- Function: `bcmbca_hsspi_suspend()`.
- Mainline commit: [`faa878d4805c`](https://github.com/torvalds/linux/commit/faa878d4805ca375738294bc52eae310eb287b16).
- Submission: [patch email](https://patch.msgid.link/tencent_54F5634545908FBA724E758054BF03953808@qq.com).

## Bug

`bcmbca_hsspi_suspend()` continued to disable the PLL and controller clocks
after `spi_controller_suspend()` failed. The callback then returned success,
hiding the failure from the power-management framework.

## Fix

Return the controller suspend error before disabling either clock.
