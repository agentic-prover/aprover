/* CBMC harness for: archive_match_exclude_entry */
#include "/tmp/libarchive_seedhunt_full/archive_match.c"

int main(void) {
    /* Allocate archive_match structure */
    struct archive_match *a = malloc(sizeof(struct archive_match));
    __CPROVER_assume(a != NULL);
    
    /* Initialize critical fields to match archive_match_new() */
    a->archive.magic = 0xcad11c9U;
    a->archive.state = 1U;
    a->recursive_include = 1;
    
    /* Initialize the exclusion_tree and exclusion_entry_list that add_entry uses */
    __archive_rb_tree_init(&(a->exclusion_tree), &rb_ops);
    entry_list_init(&(a->exclusion_entry_list));
    
    /* Use external archive_entry - CBMC will treat it as opaque */
    struct archive_entry *entry;
    
    /* Create flag parameter - must be valid combination per validate_time_flag */
    int flag;
    
    /* Time flags: ARCHIVE_MATCH_MTIME (0x0100) or ARCHIVE_MATCH_CTIME (0x0200) or both */
    /* Comparison flags: ARCHIVE_MATCH_NEWER (0x0001), ARCHIVE_MATCH_OLDER (0x0002), 
       or ARCHIVE_MATCH_EQUAL (0x0010), or combinations */
    /* Valid combinations observed from validate_time_flag logic */
    __CPROVER_assume(
        (flag & 0xff00) == 0x0100 || 
        (flag & 0xff00) == 0x0200 || 
        (flag & 0xff00) == 0x0300
    );
    __CPROVER_assume(
        (flag & 0x00ff) == 0x0001 || 
        (flag & 0x00ff) == 0x0002 || 
        (flag & 0x00ff) == 0x0010 ||
        (flag & 0x00ff) == 0x0003 ||
        (flag & 0x00ff) == 0x0011 ||
        (flag & 0x00ff) == 0x0012 ||
        (flag & 0x00ff) == 0x0013
    );
    
    /* Call the function under test */
    archive_match_exclude_entry(&(a->archive), flag, entry);
    
    return 0;
}
