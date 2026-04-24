/*
 * VibeOS ELF64 Loader
 */

#include "elf.h"
#include "string.h"
#include "printf.h"
#include <stddef.h>

int elf_validate(const void *data, size_t size) {
    if (size < sizeof(Elf64_Ehdr)) {
        return -1;
    }

    const Elf64_Ehdr *ehdr = (const Elf64_Ehdr *)data;

    // Check magic
    if (ehdr->e_ident[EI_MAG0] != 0x7F ||
        ehdr->e_ident[EI_MAG1] != 'E' ||
        ehdr->e_ident[EI_MAG2] != 'L' ||
        ehdr->e_ident[EI_MAG3] != 'F') {
        return -2;
    }

    // Check class (64-bit)
    if (ehdr->e_ident[EI_CLASS] != ELFCLASS64) {
        return -3;
    }

    // Check endianness (little endian)
    if (ehdr->e_ident[EI_DATA] != ELFDATA2LSB) {
        return -4;
    }

    // Check machine type (AArch64)
    if (ehdr->e_machine != EM_AARCH64) {
        return -5;
    }

    // Check type (executable or PIE)
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

    // printf("[ELF] Loading %d program headers\n", ehdr->e_phnum);

    // Process program headers
    for (int i = 0; i < ehdr->e_phnum; i++) {
        const Elf64_Phdr *phdr = (const Elf64_Phdr *)(base + ehdr->e_phoff + i * ehdr->e_phentsize);

        // Only load PT_LOAD segments
        if (phdr->p_type != PT_LOAD) {
            continue;
        }

        // printf("[ELF] LOAD: vaddr=0x%lx filesz=0x%lx memsz=0x%lx\n",
        //        phdr->p_vaddr, phdr->p_filesz, phdr->p_memsz);

        // Copy segment data
        void *dest = (void *)phdr->p_vaddr;
        // printf("[ELF] Copying %lu bytes to %p\n", phdr->p_filesz, dest);
        const void *src = base + phdr->p_offset;

        // Copy file contents
        if (phdr->p_filesz > 0) {
            memcpy(dest, src, phdr->p_filesz);
        }

        // Zero out any remaining memory (BSS)
        if (phdr->p_memsz > phdr->p_filesz) {
            memset((uint8_t *)dest + phdr->p_filesz, 0,
                   phdr->p_memsz - phdr->p_filesz);
        }
    }

    // printf("[ELF] Entry point: 0x%lx\n", ehdr->e_entry);

    // Debug: dump first few instructions at entry
    // uint32_t *code = (uint32_t *)ehdr->e_entry;
    // printf("[ELF] Code at entry: %08x %08x %08x %08x\n",
    //        code[0], code[1], code[2], code[3]);

    return ehdr->e_entry;
}

// Calculate total memory size needed for all LOAD segments
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

// Process dynamic relocations for PIE binaries
static void elf_process_relocations(uint64_t load_base, const Elf64_Dyn *dynamic) {
    uint64_t rela_addr = 0;
    uint64_t rela_size = 0;
    uint64_t rela_ent = sizeof(Elf64_Rela);

    // Parse dynamic section to find RELA info
    for (const Elf64_Dyn *dyn = dynamic; dyn->d_tag != DT_NULL; dyn++) {
        switch (dyn->d_tag) {
            case DT_RELA:    rela_addr = dyn->d_val; break;
            case DT_RELASZ:  rela_size = dyn->d_val; break;
            case DT_RELAENT: rela_ent = dyn->d_val;  break;
        }
    }

    if (rela_addr == 0 || rela_size == 0) {
        // printf("[ELF] No relocations to process\n");
        return;
    }

    // Relocations are at file offset, add load_base to get runtime address
    const Elf64_Rela *rela = (const Elf64_Rela *)(load_base + rela_addr);
    int num_relas = rela_size / rela_ent;

    // printf("[ELF] Processing %d relocations at 0x%lx (rela_addr=0x%lx)\n", num_relas, load_base + rela_addr, rela_addr);

    int applied = 0;
    for (int i = 0; i < num_relas; i++) {
        uint64_t offset = rela[i].r_offset;
        uint64_t type = rela[i].r_info & 0xFFFFFFFF;
        int64_t addend = rela[i].r_addend;

        if (type == R_AARCH64_RELATIVE) {
            // R_AARCH64_RELATIVE: *(load_base + offset) = load_base + addend
            uint64_t *target = (uint64_t *)(load_base + offset);
            *target = load_base + addend;
            applied++;
            // Debug first and last few
            // if (i < 3 || i >= num_relas - 3) {
            //     printf("[ELF] Reloc %d: offset=0x%lx target=0x%lx value=0x%lx\n",
            //            i, offset, (uint64_t)target, *target);
            // }
        } else {
            printf("[ELF] Unknown relocation type 0x%lx at offset 0x%lx\n", type, offset);
        }
    }

    // printf("[ELF] Applied %d relocations successfully\n", applied);
}

// Load ELF at a specific base address
int elf_load_at(const void *data, size_t size, uint64_t load_base, elf_load_info_t *info) {
    int valid = elf_validate(data, size);
    if (valid != 0) {
        printf("[ELF] Invalid ELF: error %d\n", valid);
        return -1;
    }

    const Elf64_Ehdr *ehdr = (const Elf64_Ehdr *)data;
    const uint8_t *base = (const uint8_t *)data;
    int is_pie = (ehdr->e_type == ET_DYN);

    // printf("[ELF] Loading %s at 0x%lx (%d program headers)\n",
    //        is_pie ? "PIE" : "EXEC", load_base, ehdr->e_phnum);

    uint64_t total_size = 0;
    const Elf64_Dyn *dynamic = NULL;

    // Process program headers
    for (int i = 0; i < ehdr->e_phnum; i++) {
        const Elf64_Phdr *phdr = (const Elf64_Phdr *)(base + ehdr->e_phoff + i * ehdr->e_phentsize);

        // Remember DYNAMIC segment for relocations
        if (phdr->p_type == PT_DYNAMIC) {
            // Dynamic section will be loaded, remember its runtime address
            dynamic = (const Elf64_Dyn *)(load_base + phdr->p_vaddr);
            continue;
        }

        if (phdr->p_type != PT_LOAD) continue;

        // For PIE, add load_base to vaddr
        // For EXEC, use vaddr as-is
        uint64_t dest_addr = is_pie ? (load_base + phdr->p_vaddr) : phdr->p_vaddr;

        // printf("[ELF] LOAD: vaddr=0x%lx -> 0x%lx filesz=0x%lx memsz=0x%lx\n",
        //        phdr->p_vaddr, dest_addr, phdr->p_filesz, phdr->p_memsz);

        void *dest = (void *)dest_addr;
        const void *src = base + phdr->p_offset;

        // Copy file contents
        if (phdr->p_filesz > 0) {
            memcpy(dest, src, phdr->p_filesz);
        }

        // Zero BSS
        if (phdr->p_memsz > phdr->p_filesz) {
            // uint64_t bss_start = dest_addr + phdr->p_filesz;
            uint64_t bss_size = phdr->p_memsz - phdr->p_filesz;
            // printf("[ELF] Zeroing BSS: 0x%lx - 0x%lx (size 0x%lx)\n",
            //        bss_start, bss_start + bss_size, bss_size);
            memset((uint8_t *)dest + phdr->p_filesz, 0, bss_size);
        }

        uint64_t seg_end = phdr->p_vaddr + phdr->p_memsz;
        if (seg_end > total_size) total_size = seg_end;
    }

    // Process relocations for PIE binaries
    if (is_pie && dynamic) {
        elf_process_relocations(load_base, dynamic);
    }

    // Calculate entry point
    uint64_t entry = is_pie ? (load_base + ehdr->e_entry) : ehdr->e_entry;

    // printf("[ELF] Entry point: 0x%lx\n", entry);

    // Fill info struct
    if (info) {
        info->entry = entry;
        info->load_base = load_base;
        info->load_size = total_size;
    }

    return 0;
}
