/* CBMC harness for: cmp_node_mbs */
#include "/tmp/libarchive_seedhunt_full/archive_match.c"
#include <stdlib.h>

int main(void) {
    /* Allocate two match_file structures */
    struct match_file *f1 = malloc(sizeof(struct match_file));
    struct match_file *f2 = malloc(sizeof(struct match_file));
    
    __CPROVER_assume(f1 != NULL);
    __CPROVER_assume(f2 != NULL);
    
    /* Initialize the archive_mstring structures within match_file.
     * The archive_mstring_get_mbs function is external and will be havoc'd,
     * so we don't need to fully initialize the mstring internals.
     * However, we should zero-initialize to avoid undefined behavior. */
    f1->pathname.aes_mbs.s = NULL;
    f1->pathname.aes_mbs.length = 0;
    f1->pathname.aes_mbs.buffer_length = 0;
    f1->pathname.aes_utf8.s = NULL;
    f1->pathname.aes_utf8.length = 0;
    f1->pathname.aes_utf8.buffer_length = 0;
    f1->pathname.aes_wcs.s = NULL;
    f1->pathname.aes_wcs.length = 0;
    f1->pathname.aes_wcs.buffer_length = 0;
    f1->pathname.aes_mbs_in_locale.s = NULL;
    f1->pathname.aes_mbs_in_locale.length = 0;
    f1->pathname.aes_mbs_in_locale.buffer_length = 0;
    f1->pathname.aes_set = 0;
    
    f2->pathname.aes_mbs.s = NULL;
    f2->pathname.aes_mbs.length = 0;
    f2->pathname.aes_mbs.buffer_length = 0;
    f2->pathname.aes_utf8.s = NULL;
    f2->pathname.aes_utf8.length = 0;
    f2->pathname.aes_utf8.buffer_length = 0;
    f2->pathname.aes_wcs.s = NULL;
    f2->pathname.aes_wcs.length = 0;
    f2->pathname.aes_wcs.buffer_length = 0;
    f2->pathname.aes_mbs_in_locale.s = NULL;
    f2->pathname.aes_mbs_in_locale.length = 0;
    f2->pathname.aes_mbs_in_locale.buffer_length = 0;
    f2->pathname.aes_set = 0;
    
    /* Cast to archive_rb_node pointers as the function expects */
    const struct archive_rb_node *n1 = (const struct archive_rb_node *)f1;
    const struct archive_rb_node *n2 = (const struct archive_rb_node *)f2;
    
    /* Call the function under test */
    int result = cmp_node_mbs(n1, n2);
    
    return 0;
}
