#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    struct archive *a;
    struct archive_entry *entry;
    int r;

    /* Create archive match object */
    a = archive_match_new();
    if (a == NULL) {
        fprintf(stderr, "Failed to create archive_match\n");
        return 1;
    }

    /* Add an inclusion pattern to trigger path matching */
    r = archive_match_include_pattern(a, "*.txt");
    if (r != ARCHIVE_OK) {
        fprintf(stderr, "Failed to add inclusion pattern\n");
        archive_match_free(a);
        return 1;
    }

    /* Create an archive entry with NULL pathname */
    entry = archive_entry_new();
    if (entry == NULL) {
        fprintf(stderr, "Failed to create archive_entry\n");
        archive_match_free(a);
        return 1;
    }

    /* Explicitly set pathname to NULL to simulate malformed archive entry */
    archive_entry_set_pathname(entry, NULL);

    /* Verify pathname is actually NULL */
    const char *pathname = archive_entry_pathname(entry);
    if (pathname != NULL) {
        fprintf(stderr, "Pathname is not NULL, cannot reproduce bug\n");
        archive_entry_free(entry);
        archive_match_free(a);
        return 1;
    }

    /* Call archive_match_excluded which will call path_excluded with NULL pathname */
    /* This should trigger NULL pointer dereference in match_path_inclusion */
    r = archive_match_excluded(a, entry);

    /* If we reach here, the bug was not triggered */
    fprintf(stderr, "No crash occurred, bug not reproduced\n");
    
    archive_entry_free(entry);
    archive_match_free(a);
    return 0;
}