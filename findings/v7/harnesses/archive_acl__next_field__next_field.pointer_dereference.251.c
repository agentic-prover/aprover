/* CBMC harness for: next_field */
#include "/tmp/libarchive_seedhunt_full/archive_acl.c"

int main(void) {
    /* Allocate a non-deterministic string buffer */
    size_t length;
    __CPROVER_assume(length > 0 && length <= 1024);
    
    char *buffer = malloc(length);
    __CPROVER_assume(buffer != NULL);
    
    /* Make it a valid string (can contain any characters) */
    for (size_t i = 0; i < length; i++) {
        buffer[i] = __CPROVER_nondet_char();
    }
    
    /* Set up pointers for next_field */
    const char *p = buffer;
    size_t l = length;
    const char *start;
    const char *end;
    char sep;
    
    /* Call the function under test */
    next_field(&p, &l, &start, &end, &sep);
    
    free(buffer);
    return 0;
}
