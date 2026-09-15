# spi: fsl-dspi: clean up after failed suspend and resume

BMC-Agent discovered a suspend error-handling bug in the fsl-dspi driver.
Jiawen Liu submitted a patch that fixes the suspend failure path and related
resume cleanup. The patch was accepted into Linux mainline.

- Source: [`drivers/spi/spi-fsl-dspi.c`](https://github.com/torvalds/linux/blob/2543355f3da56a297e3803ccb5d29f4dce5f18f1/drivers/spi/spi-fsl-dspi.c).
- Functions: `dspi_suspend()`, `dspi_resume()`.
- Mainline commit: [`2543355f3da5`](https://github.com/torvalds/linux/commit/2543355f3da56a297e3803ccb5d29f4dce5f18f1).
- Submission: [patch email](https://patch.msgid.link/tencent_427FA55E3D59112524886E9C931CA0F92F06@qq.com).

## Bug

`dspi_suspend()` disabled the IRQ before calling `spi_controller_suspend()`.
If controller suspension failed, it left the IRQ disabled, continued with
clock and pinctrl teardown, and returned success.

The resume path also ignored the controller resume result and did not
disable the clock when subsequent initialization failed.

## Fix

Restore the IRQ and return immediately if controller suspension fails.
During resume, propagate controller resume and initialization errors through
a common clock-cleanup path.
