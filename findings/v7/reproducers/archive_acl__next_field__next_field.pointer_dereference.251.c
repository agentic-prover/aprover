#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    struct archive_entry *entry;
    char *acl_text;
    size_t text_len;
    int i;
    
    entry = archive_entry_new();
    if (!entry) {
        fprintf(stderr, "Failed to create archive entry\n");
        return 1;
    }
    
    text_len = 8192;
    acl_text = (char *)malloc(text_len);
    if (!acl_text) {
        archive_entry_free(entry);
        return 1;
    }
    
    memset(acl_text, 'A', text_len - 1);
    acl_text[text_len - 1] = '\0';
    
    for (i = 0; i < text_len - 2; i += 20) {
        if (i + 19 < text_len - 1) {
            memcpy(acl_text + i, "user:root:rwx,", 14);
        }
    }
    
    acl_text[text_len - 2] = '#';
    acl_text[text_len - 1] = '\0';
    
    int ret = archive_entry_acl_from_text(entry, acl_text, ARCHIVE_ENTRY_ACL_TYPE_ACCESS);
    
    if (ret != ARCHIVE_OK) {
        fprintf(stderr, "ACL parsing returned error: %d\n", ret);
    }
    
    free(acl_text);
    archive_entry_free(entry);
    
    return 0;
}