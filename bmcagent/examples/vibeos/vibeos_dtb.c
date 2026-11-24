/*
 * VibeOS DTB Parser — self-contained for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/dtb.c
 * Changes: stubbed printf; inlined strlen; removed kernel headers.
 *
 * Key properties CBMC checks:
 *  - read_be32/read_be64: never read past dtb buffer bounds
 *  - dtb_parse: offset arithmetic stays within totalsize
 *  - align4: no integer overflow on offset + 3
 */

#include <stdint.h>
#include <stddef.h>

/* stub for kernel printf */
static void printf(const char *fmt, ...) { (void)fmt; }

/* --- DTB constants --- */
#define DTB_MAGIC       0xd00dfeed
#define FDT_BEGIN_NODE  0x00000001
#define FDT_END_NODE    0x00000002
#define FDT_PROP        0x00000003
#define FDT_NOP         0x00000004
#define FDT_END         0x00000009

struct dtb_memory_info {
    uint64_t base;
    uint64_t size;
};

static const char *dtb_error = "No error";

static uint32_t read_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static uint64_t read_be64(const uint8_t *p) {
    return ((uint64_t)p[0] << 56) | ((uint64_t)p[1] << 48) |
           ((uint64_t)p[2] << 40) | ((uint64_t)p[3] << 32) |
           ((uint64_t)p[4] << 24) | ((uint64_t)p[5] << 16) |
           ((uint64_t)p[6] << 8) | (uint64_t)p[7];
}

static uint32_t align4(uint32_t offset) {
    return (offset + 3) & ~3;
}

static int str_eq(const char *a, const char *b) {
    while (*a && *b) {
        if (*a != *b) return 0;
        a++;
        b++;
    }
    return *a == *b;
}

static int str_starts_with(const char *str, const char *prefix) {
    while (*prefix) {
        if (*str != *prefix) return 0;
        str++;
        prefix++;
    }
    return 1;
}

/* local strlen to avoid pulling in string.h which conflicts with VibeOS's own */
static size_t local_strlen(const char *s) {
    size_t len = 0;
    while (s[len]) len++;
    return len;
}

const char *dtb_get_error(void) {
    return dtb_error;
}

int dtb_parse(void *dtb_addr, struct dtb_memory_info *mem_info) {
    uint8_t *hdr = (uint8_t *)dtb_addr;

    mem_info->base = 0;
    mem_info->size = 0;

    uint32_t magic = read_be32(hdr + 0x00);
    if (magic != DTB_MAGIC) {
        dtb_error = "Invalid DTB magic";
        printf("[DTB] Invalid magic: 0x%x (expected 0x%x)\n", magic, DTB_MAGIC);
        return -1;
    }

    uint32_t totalsize = read_be32(hdr + 0x04);
    uint32_t off_struct = read_be32(hdr + 0x08);
    uint32_t off_strings = read_be32(hdr + 0x0C);
    uint32_t version = read_be32(hdr + 0x14);

    printf("[DTB] Found valid DTB at %p\n", dtb_addr);
    printf("[DTB] Version: %d, Size: %d bytes\n", version, totalsize);

    uint8_t *base = (uint8_t *)dtb_addr;
    uint8_t *struct_block = base + off_struct;
    char *strings_block = (char *)(base + off_strings);

    uint32_t offset = 0;
    int depth = 0;
    int in_memory_node = 0;
    int in_root = 0;
    uint32_t root_addr_cells = 2;
    uint32_t root_size_cells = 1;

    while (1) {
        uint32_t token = read_be32(struct_block + offset);
        offset += 4;

        if (token == FDT_END) {
            break;
        }

        switch (token) {
            case FDT_BEGIN_NODE: {
                char *name = (char *)(struct_block + offset);
                uint32_t name_len = local_strlen(name) + 1;
                offset = align4(offset + name_len);

                depth++;

                if (depth == 1 && name[0] == '\0') {
                    in_root = 1;
                }

                if (depth == 2 && (str_eq(name, "memory") || str_starts_with(name, "memory@"))) {
                    in_memory_node = 1;
                    printf("[DTB] Found memory node: %s\n", name[0] ? name : "(root)");
                }
                break;
            }

            case FDT_END_NODE: {
                if (depth == 2) {
                    in_memory_node = 0;
                }
                if (depth == 1) {
                    in_root = 0;
                }
                depth--;
                break;
            }

            case FDT_PROP: {
                uint32_t len = read_be32(struct_block + offset);
                offset += 4;
                uint32_t nameoff = read_be32(struct_block + offset);
                offset += 4;

                char *prop_name = strings_block + nameoff;
                uint8_t *prop_value = struct_block + offset;

                if (in_root && depth == 1) {
                    if (str_eq(prop_name, "#address-cells") && len == 4) {
                        root_addr_cells = read_be32(prop_value);
                    } else if (str_eq(prop_name, "#size-cells") && len == 4) {
                        root_size_cells = read_be32(prop_value);
                    }
                }

                if (in_memory_node && str_eq(prop_name, "reg")) {
                    printf("[DTB] Memory reg: addr_cells=%d, size_cells=%d, len=%d\n",
                           root_addr_cells, root_size_cells, len);

                    if (root_addr_cells == 2) {
                        mem_info->base = read_be64(prop_value);
                        prop_value += 8;
                    } else {
                        mem_info->base = read_be32(prop_value);
                        prop_value += 4;
                    }

                    if (root_size_cells == 2) {
                        mem_info->size = read_be64(prop_value);
                    } else {
                        mem_info->size = read_be32(prop_value);
                    }

                    printf("[DTB] Memory: base=0x%lx, size=0x%lx (%lu MB)\n",
                           mem_info->base, mem_info->size, mem_info->size / (1024 * 1024));
                }

                offset = align4(offset + len);
                break;
            }

            case FDT_NOP: {
                break;
            }

            default: {
                dtb_error = "Unknown token in DTB";
                printf("[DTB] Unknown token: 0x%x at offset %d\n", token, offset - 4);
                return -1;
            }
        }
    }

    if (mem_info->size == 0) {
        dtb_error = "No memory node found";
        return -1;
    }

    dtb_error = "Success";
    return 0;
}
