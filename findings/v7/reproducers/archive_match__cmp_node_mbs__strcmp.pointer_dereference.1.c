#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <locale.h>

int main(void) {
    struct archive *a;
    struct archive_entry *entry1, *entry2;
    int r;
    
    /* Set a locale that will cause conversion failures for invalid sequences */
    setlocale(LC_ALL, "en_US.UTF-8");
    
    a = archive_read_new();
    if (a == NULL) {
        return 1;
    }
    
    archive_read_support_filter_all(a);
    archive_read_support_format_all(a);
    
    /* Create an in-memory archive with entries containing invalid UTF-8 sequences */
    struct archive *writer = archive_write_new();
    archive_write_set_format_pax(writer);
    archive_write_add_filter_none(writer);
    
    char *buffer = NULL;
    size_t buffer_size = 0;
    archive_write_open_memory(writer, &buffer, &buffer_size, &buffer_size);
    
    /* Create entries with invalid multi-byte sequences in pathnames */
    entry1 = archive_entry_new();
    /* Invalid UTF-8: 0xFF is not valid in UTF-8 */
    char invalid_path1[] = {0xFF, 0xFE, 0xFD, 0x00};
    archive_entry_set_pathname(entry1, invalid_path1);
    archive_entry_set_size(entry1, 0);
    archive_entry_set_filetype(entry1, AE_IFREG);
    archive_entry_set_perm(entry1, 0644);
    archive_write_header(writer, entry1);
    archive_entry_free(entry1);
    
    entry2 = archive_entry_new();
    /* Another invalid UTF-8 sequence */
    char invalid_path2[] = {0xC0, 0x80, 0x00};
    archive_entry_set_pathname(entry2, invalid_path2);
    archive_entry_set_size(entry2, 0);
    archive_entry_set_filetype(entry2, AE_IFREG);
    archive_entry_set_perm(entry2, 0644);
    archive_write_header(writer, entry2);
    archive_entry_free(entry2);
    
    archive_write_close(writer);
    archive_write_free(writer);
    
    /* Now read the archive with matching enabled */
    r = archive_read_open_memory(a, buffer, buffer_size);
    if (r != ARCHIVE_OK) {
        archive_read_free(a);
        free(buffer);
        return 1;
    }
    
    /* Enable matching - this will use the red-black tree with cmp_node_mbs */
    struct archive *match = archive_match_new();
    if (match == NULL) {
        archive_read_free(a);
        free(buffer);
        return 1;
    }
    
    /* Add exclusion patterns that will trigger tree operations */
    archive_match_exclude_pattern(match, "*.txt");
    
    /* Read entries - this should trigger the tree comparison with NULL pointers */
    struct archive_entry *entry;
    while (archive_read_next_header(a, &entry) == ARCHIVE_OK) {
        /* Check if entry is excluded - this triggers tree search with cmp_node_mbs */
        archive_match_excluded(match, entry);
    }
    
    archive_match_free(match);
    archive_read_free(a);
    free(buffer);
    
    return 0;
}