# spi: nxp-fspi: disable runtime PM on probe failures

BMC-Agent discovered a runtime PM cleanup bug in the nxp-fspi probe path.
Jiawen Liu submitted the fix, which was accepted into Linux mainline.

- Source: [`drivers/spi/spi-nxp-fspi.c`](https://github.com/torvalds/linux/blob/5d14285d60ba95561609a293064654a0c7d7d928/drivers/spi/spi-nxp-fspi.c).
- Function: `nxp_fspi_probe()`.
- Mainline commit: [`5d14285d60ba`](https://github.com/torvalds/linux/commit/5d14285d60ba95561609a293064654a0c7d7d928).
- Submission: [patch email](https://patch.msgid.link/tencent_8FC0B8DFAF4AE67AEBA20548045D53A77707@qq.com).

## Bug

`nxp_fspi_probe()` enabled runtime PM before operations that could fail,
but registered its managed cleanup action later. Early probe failures could
therefore leave runtime PM enabled. A failed `pm_runtime_get_sync()` also
left the usage counter incremented.

## Fix

Use `pm_runtime_resume_and_get()`, which balances the usage counter on
failure, and route early probe errors through cleanup that disables
autosuspend and runtime PM.
