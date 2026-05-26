/* CBMC harness for: match_owner_name_mbs */
#include "/tmp/libarchive_seedhunt_full/archive_match.c"

int main(void) {
    /* Allocate archive_match structure */
    struct archive_match *a = malloc(sizeof(struct archive_match));
    __CPROVER_assume(a != NULL);
    
    /* Initialize archive structure fields that might be accessed */
    a->archive.magic = 0xdeb0c5U;
    a->archive.state = 1;
    
    /* Allocate match_list */
    struct match_list *list = malloc(sizeof(struct match_list));
    __CPROVER_assume(list != NULL);
    
    /* Create a linked list of 0-3 match entries */
    unsigned int num_matches;
    __CPROVER_assume(num_matches <= 3);
    
    struct match *prev = NULL;
    struct match *first = NULL;
    
    for (unsigned int i = 0; i < num_matches; i++) {
        struct match *m = malloc(sizeof(struct match));
        __CPROVER_assume(m != NULL);
        
        m->next = NULL;
        m->matched = 0;
        
        /* Initialize the pattern mstring */
        m->pattern.aes_set = 0;
        m->pattern.aes_mbs.s = NULL;
        m->pattern.aes_mbs.length = 0;
        m->pattern.aes_mbs.buffer_length = 0;
        m->pattern.aes_utf8.s = NULL;
        m->pattern.aes_utf8.length = 0;
        m->pattern.aes_utf8.buffer_length = 0;
        m->pattern.aes_wcs.s = NULL;
        m->pattern.aes_wcs.length = 0;
        m->pattern.aes_wcs.buffer_length = 0;
        m->pattern.aes_mbs_in_locale.s = NULL;
        m->pattern.aes_mbs_in_locale.length = 0;
        m->pattern.aes_mbs_in_locale.buffer_length = 0;
        
        if (prev == NULL) {
            first = m;
        } else {
            prev->next = m;
        }
        prev = m;
    }
    
    list->first = first;
    
    /* Create input name string */
    unsigned int name_len;
    __CPROVER_assume(name_len < 256);
    
    char *name = NULL;
    if (name_len > 0) {
        name = malloc(name_len + 1);
        __CPROVER_assume(name != NULL);
        for (unsigned int i = 0; i < name_len; i++) {
            name[i] = nondet_char();
            __CPROVER_assume(name[i] != '\0');
        }
        name[name_len] = '\0';
    }
    
    /* Call the function under test */
    int result = match_owner_name_mbs(a, list, name);
    
    return 0;
}
