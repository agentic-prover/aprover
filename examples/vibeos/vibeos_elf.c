/*
 * VibeOS ELF64 loader — self-contained for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/elf.c
 * Changes: stubbed printf; kept all ELF logic intact.
 *
 * Key properties CBMC checks:
 *  - e_phoff + i*e_phentsize never reads past the data buffer
 *  - p_offset + p_filesz never reads past the data buffer
 *  - elf_calc_size: p_vaddr + p_memsz never overflows uint64_t
 *  - elf_process_relocations: rela_ent != 0 before division
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

/* ---- stub for kernel printf ---- */
static void printf(const char *fmt, ...) { (void)fmt; }

/* ---- elf.h inlined ---- */

typedef struct {
    uint8_t  e_ident[16];
    uint16_t e_type;
    uint16_t e_machine;
    uint32_t e_version;
    uint64_t e_entry;
    uint64_t e_phoff;
    uint64_t e_shoff;
    uint32_t e_flags;
    uint16_t e_ehsize;
    uint16_t e_phentsize;
    uint16_t e_phnum;
    uint16_t e_shentsize;
    uint16_t e_shnum;
    uint16_t e_shstrndx;
} Elf64_Ehdr;

typedef struct {
    uint32_t p_type;
    uint32_t p_flags;
    uint64_t p_offset;
    uint64_t p_vaddr;
    uint64_t p_paddr;
    uint64_t p_filesz;
    uint64_t p_memsz;
    uint64_t p_align;
} Elf64_Phdr;

typedef struct {
    int64_t  d_tag;
    uint64_t d_val;
} Elf64_Dyn;

typedef struct {
    uint64_t r_offset;
    uint64_t r_info;
    int64_t  r_addend;
} Elf64_Rela;

typedef struct {
    uint64_t entry;
    uint64_t load_base;
    uint64_t load_size;
} elf_load_info_t;

#define PT_NULL    0
#define PT_LOAD    1
#define PT_DYNAMIC 2
#define PT_INTERP  3
#define DT_NULL    0
#define DT_RELA    7
#define DT_RELASZ  8
#define DT_RELAENT 9
#define R_AARCH64_RELATIVE 0x403
#define EI_MAG0    0
#define EI_MAG1    1
#define EI_MAG2    2
#define EI_MAG3    3
#define EI_CLASS   4
#define EI_DATA    5
#define ELFCLASS64   2
#define ELFDATA2LSB  1
#define EM_AARCH64   183
#define ET_EXEC 2
#define ET_DYN  3

/* ---- original elf.c (unmodified logic) ---- */

int elf_validate(const void *data, size_t size) {
    if (size < sizeof(Elf64_Ehdr)) {
        return -1;
    }

    const Elf64_Ehdr *ehdr = (const Elf64_Ehdr *)data;

    if (ehdr->e_ident[EI_MAG0] != 0x7F ||
        ehdr->e_ident[EI_MAG1] != 'E' ||
        ehdr->e_ident[EI_MAG2] != 'L' ||
        ehdr->e_ident[EI_MAG3] != 'F') {
        return -2;
    }

    if (ehdr->e_ident[EI_CLASS] != ELFCLASS64) {
        return -3;
    }

    if (ehdr->e_ident[EI_DATA] != ELFDATA2LSB) {
        return -4;
    }

    if (ehdr->e_machine != EM_AARCH64) {
        return -5;
    }

    if (ehdr->e_type != ET_EXEC && ehdr->e_type != ET_DYN) {
        return -6;
    }

    return 0;
}

uint64_t elf_entry(const void *data) {
    const Elf64_Ehdr *ehdr = (const Elf64_Ehdr *)data;
    return ehdr->e_entry;
}

uint64_t elf_load(const void *data, size_t size) {
    int valid = elf_validate(data, size);
    if (valid != 0) {
        printf("[ELF] Invalid ELF: error %d\n", valid);
        return 0;
    }

    const Elf64_Ehdr *ehdr = (const Elf64_Ehdr *)data;
    const uint8_t *base = (const uint8_t *)data;

    for (int i = 0; i < ehdr->e_phnum; i++) {
        const Elf64_Phdr *phdr = (const Elf64_Phdr *)(base + ehdr->e_phoff + i * ehdr->e_phentsize);

        if (phdr->p_type != PT_LOAD) {
            continue;
        }

        void *dest = (void *)phdr->p_vaddr;
        const void *src = base + phdr->p_offset;

        if (phdr->p_filesz > 0) {
            memcpy(dest, src, phdr->p_filesz);
        }

        if (phdr->p_memsz > phdr->p_filesz) {
            memset((uint8_t *)dest + phdr->p_filesz, 0,
                   phdr->p_memsz - phdr->p_filesz);
        }
    }

    return ehdr->e_entry;
}

uint64_t elf_calc_size(const void *data, size_t size) {
    int valid = elf_validate(data, size);
    if (valid != 0) return 0;

    const Elf64_Ehdr *ehdr = (const Elf64_Ehdr *)data;
    const uint8_t *base = (const uint8_t *)data;

    uint64_t min_addr = (uint64_t)-1;
    uint64_t max_addr = 0;

    for (int i = 0; i < ehdr->e_phnum; i++) {
        const Elf64_Phdr *phdr = (const Elf64_Phdr *)(base + ehdr->e_phoff + i * ehdr->e_phentsize);
        if (phdr->p_type != PT_LOAD) continue;

        if (phdr->p_vaddr < min_addr) {
            min_addr = phdr->p_vaddr;
        }
        uint64_t end = phdr->p_vaddr + phdr->p_memsz;
        if (end > max_addr) {
            max_addr = end;
        }
    }

    if (max_addr <= min_addr) return 0;
    return max_addr - min_addr;
}

static void elf_process_relocations(uint64_t load_base, const Elf64_Dyn *dynamic) {
    uint64_t rela_addr = 0;
    uint64_t rela_size = 0;
    uint64_t rela_ent = sizeof(Elf64_Rela);

    for (const Elf64_Dyn *dyn = dynamic; dyn->d_tag != DT_NULL; dyn++) {
        switch (dyn->d_tag) {
            case DT_RELA:    rela_addr = dyn->d_val; break;
            case DT_RELASZ:  rela_size = dyn->d_val; break;
            case DT_RELAENT: rela_ent = dyn->d_val;  break;
        }
    }

    if (rela_addr == 0 || rela_size == 0) {
        return;
    }

    const Elf64_Rela *rela = (const Elf64_Rela *)(load_base + rela_addr);
    int num_relas = rela_size / rela_ent;

    int applied = 0;
    for (int i = 0; i < num_relas; i++) {
        uint64_t offset = rela[i].r_offset;
        uint64_t type = rela[i].r_info & 0xFFFFFFFF;
        int64_t addend = rela[i].r_addend;

        if (type == R_AARCH64_RELATIVE) {
            uint64_t *target = (uint64_t *)(load_base + offset);
            *target = load_base + addend;
            applied++;
        } else {
            printf("[ELF] Unknown relocation type 0x%lx at offset 0x%lx\n", type, offset);
        }
    }
}

int elf_load_at(const void *data, size_t size, uint64_t load_base, elf_load_info_t *info) {
    int valid = elf_validate(data, size);
    if (valid != 0) {
        printf("[ELF] Invalid ELF: error %d\n", valid);
        return -1;
    }

    const Elf64_Ehdr *ehdr = (const Elf64_Ehdr *)data;
    const uint8_t *base = (const uint8_t *)data;
    int is_pie = (ehdr->e_type == ET_DYN);

    uint64_t total_size = 0;
    const Elf64_Dyn *dynamic = NULL;

    for (int i = 0; i < ehdr->e_phnum; i++) {
        const Elf64_Phdr *phdr = (const Elf64_Phdr *)(base + ehdr->e_phoff + i * ehdr->e_phentsize);

        if (phdr->p_type == PT_DYNAMIC) {
            dynamic = (const Elf64_Dyn *)(load_base + phdr->p_vaddr);
            continue;
        }

        if (phdr->p_type != PT_LOAD) continue;

        uint64_t dest_addr = is_pie ? (load_base + phdr->p_vaddr) : phdr->p_vaddr;

        void *dest = (void *)dest_addr;
        const void *src = base + phdr->p_offset;

        if (phdr->p_filesz > 0) {
            memcpy(dest, src, phdr->p_filesz);
        }

        if (phdr->p_memsz > phdr->p_filesz) {
            uint64_t bss_size = phdr->p_memsz - phdr->p_filesz;
            memset((uint8_t *)dest + phdr->p_filesz, 0, bss_size);
        }

        uint64_t seg_end = phdr->p_vaddr + phdr->p_memsz;
        if (seg_end > total_size) total_size = seg_end;
    }

    if (is_pie && dynamic) {
        elf_process_relocations(load_base, dynamic);
    }

    uint64_t entry = is_pie ? (load_base + ehdr->e_entry) : ehdr->e_entry;

    if (info) {
        info->entry = entry;
        info->load_base = load_base;
        info->load_size = total_size;
    }

    return 0;
}
