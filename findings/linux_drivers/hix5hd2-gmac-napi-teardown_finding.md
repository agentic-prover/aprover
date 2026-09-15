# net: hisilicon: hix5hd2_gmac: remove redundant NAPI delete

BMC-Agent discovered a NAPI teardown-order bug in the hix5hd2 GMAC driver.
Jiawen Liu submitted a fix and updated it following maintainer review.
The revised patch was accepted into Linux mainline.

- Source: [`drivers/net/ethernet/hisilicon/hix5hd2_gmac.c`](https://github.com/torvalds/linux/blob/f307a7dc32097c11413178fca437a10d20890bc2/drivers/net/ethernet/hisilicon/hix5hd2_gmac.c).
- Function: `hix5hd2_dev_remove()`.
- Mainline commit: [`f307a7dc3209`](https://github.com/torvalds/linux/commit/f307a7dc32097c11413178fca437a10d20890bc2).
- Submission: [patch email](https://patch.msgid.link/tencent_5FFD37A252B4FEA6A80AD25B17C8E904F005@qq.com).

## Bug

`hix5hd2_dev_remove()` called `netif_napi_del()` before
`unregister_netdev()`, removing the NAPI instance before the networking core
could stop the registered device. The explicit deletion was also redundant:
`free_netdev()` deletes the NAPI instances attached to the netdev.

## Fix

Remove the explicit NAPI deletion from the remove path and rely on the
normal unregister/free sequence. The probe-failure cleanup is unchanged.
