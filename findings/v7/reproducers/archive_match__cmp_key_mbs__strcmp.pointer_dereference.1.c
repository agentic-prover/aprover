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

    /* Create a new archive for writing */
    a = archive_write_new();
    if (a == NULL) {
        fprintf(stderr, "Failed to create archive\n");
        return 1;
    }

    archive_write_set_format_pax(a);
    archive_write_open_memory(a, (void **)&buff, &size, NULL);

    /* Create an entry with a pathname */
    entry = archive_entry_new();
    archive_entry_set_pathname(entry, "test.txt");
    archive_entry_set_size(entry, 0);
    archive_entry_set_filetype(entry, AE_IFREG);
    archive_entry_set_perm(entry, 0644);
    
    archive_write_header(a, entry);
    archive_entry_free(entry);
    archive_write_close(a);
    archive_write_free(a);

    /* Now read the archive and set up matching */
    a = archive_read_new();
    archive_read_support_format_all(a);
    archive_read_open_memory(a, buff, size);

    /* Create a match object */
    struct archive *match = archive_match_new();
    if (match == NULL) {
        fprintf(stderr, "Failed to create match\n");
        archive_read_free(a);
        return 1;
    }

    /* Add a pathname pattern - this will populate the internal tree */
    archive_match_include_pattern(match, "test.txt");

    /* Read the entry */
    entry = archive_entry_new();
    r = archive_read_next_header(a, &entry);
    
    /* Now try to match with a NULL pathname by creating an entry with NULL pathname */
    struct archive_entry *null_entry = archive_entry_new();
    archive_entry_set_pathname(null_entry, NULL);
    
    /* This should trigger the bug - matching with NULL pathname */
    /* The match operation will call cmp_key_mbs with NULL key */
    archive_match_path_excluded(match, null_entry);

    archive_entry_free(null_entry);
    archive_entry_free(entry);
    archive_read_free(a);
    archive_match_free(match);

    return 0;
}