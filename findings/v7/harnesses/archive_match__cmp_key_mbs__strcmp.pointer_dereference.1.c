/* CBMC harness for: cmp_key_mbs */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Forward declarations for external functions */
int archive_mstring_get_mbs(struct archive *, struct archive_mstring *, const char **);

#include "/tmp/libarchive_seedhunt_full/archive_match.c"

int main(void) {
    /* Allocate a match_file structure */
    struct match_file *f = malloc(sizeof(struct match_file));
    __CPROVER_assume(f != NULL);
    
    /* Initialize the archive_mstring pathname field */
    /* The aes_mbs field is what archive_mstring_get_mbs will access */
    size_t str_len;
    __CPROVER_assume(str_len < 1024);
    
    char *mbs_str = malloc(str_len + 1);
    if (mbs_str != NULL) {
        mbs_str[str_len] = '\0';
        f->pathname.aes_mbs.s = mbs_str;
        f->pathname.aes_mbs.length = str_len;
        f->pathname.aes_mbs.buffer_length = str_len + 1;
    } else {
        f->pathname.aes_mbs.s = NULL;
        f->pathname.aes_mbs.length = 0;
        f->pathname.aes_mbs.buffer_length = 0;
    }
    
    /* Initialize other fields to avoid undefined behavior */
    f->pathname.aes_set = 0;
    
    /* Allocate the key string */
    size_t key_len;
    __CPROVER_assume(key_len < 1024);
    char *key = malloc(key_len + 1);
    __CPROVER_assume(key != NULL);
    key[key_len] = '\0';
    
    /* Call the function under test */
    cmp_key_mbs((const struct archive_rb_node *)f, (const void *)key);
    
    return 0;
}
