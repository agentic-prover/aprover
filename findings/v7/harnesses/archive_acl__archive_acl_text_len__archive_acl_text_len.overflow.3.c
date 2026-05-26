/* CBMC harness for: archive_acl_text_len */
#include "/tmp/libarchive_seedhunt_full/archive_acl.c"

int main(void) {
    /* Allocate the archive_acl structure */
    struct archive_acl *acl = malloc(sizeof(struct archive_acl));
    __CPROVER_assume(acl != NULL);
    
    /* Initialize mode field */
    acl->mode = nondet_uint();
    acl->acl_state = nondet_int();
    acl->acl_text_w = NULL;
    acl->acl_text = NULL;
    acl->acl_types = nondet_int();
    acl->acl_p = NULL;
    
    /* Create a bounded linked list of ACL entries */
    unsigned int num_entries;
    __CPROVER_assume(num_entries <= 10);
    
    struct archive_acl_entry *prev = NULL;
    acl->acl_head = NULL;
    
    for (unsigned int i = 0; i < num_entries; i++) {
        struct archive_acl_entry *entry = malloc(sizeof(struct archive_acl_entry));
        __CPROVER_assume(entry != NULL);
        
        /* Set entry fields based on real caller constraints */
        /* tag must be one of the valid ACL tag values */
        int tag = nondet_int();
        __CPROVER_assume(tag == 10001 || tag == 10002 || tag == 10003 || 
                        tag == 10004 || tag == 10005 || tag == 10006 || 
                        tag == 10107);
        entry->tag = tag;
        
        /* type must be one of the valid ACL type values */
        int type = nondet_int();
        __CPROVER_assume(type == 0x00000100 || type == 0x00000200 || 
                        type == 0x00000400 || type == 0x00000800 || 
                        type == 0x00001000 || type == 0x00002000);
        entry->type = type;
        
        entry->permset = nondet_int();
        entry->id = nondet_int();
        __CPROVER_assume(entry->id >= 0);
        
        /* Initialize archive_mstring - set to zero for simplicity */
        memset(&entry->name, 0, sizeof(struct archive_mstring));
        
        entry->next = NULL;
        
        if (prev == NULL) {
            acl->acl_head = entry;
        } else {
            prev->next = entry;
        }
        prev = entry;
    }
    
    /* Set up parameters for archive_acl_text_len */
    int want_type = nondet_int();
    /* want_type should be one of the valid type combinations */
    __CPROVER_assume(want_type == 0x00000100 || 
                    want_type == 0x00000200 || 
                    want_type == (0x00000100 | 0x00000200) ||
                    want_type == (0x00000400 | 0x00000800 | 0x00001000 | 0x00002000));
    
    int flags = nondet_int();
    int wide = nondet_int();
    __CPROVER_assume(wide == 0 || wide == 1);
    
    struct archive *a = NULL;
    struct archive_string_conv *sc = NULL;
    
    /* Constrain wcslen return value to avoid havoc explosion */
    size_t wcslen_ret;
    __CPROVER_assume(wcslen_ret < 1024);
    
    /* Call the function under test */
    size_t result = archive_acl_text_len(acl, want_type, flags, wide, a, sc);
    
    return 0;
}
