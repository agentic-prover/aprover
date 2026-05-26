/* Auto-generated CBMC harness (real-libc mode) for: archive_match_excluded */
/* Source: /tmp/libarchive_seedhunt_full/archive_match.c */
/* Harness entry: main */

#include "/tmp/libarchive_seedhunt_full/archive_match.c"


int main(void) {
    /* Step 1: nondeterministic inputs */
    /* struct-pointer init for '_a' (struct archive, 19 fields) */
    struct archive __a_obj;
    struct archive* _a = &__a_obj;
    char ___a_obj_archive_format_name_buf[5];
    unsigned int ___a_obj_archive_format_name_len;
    __CPROVER_assume(___a_obj_archive_format_name_len <= (unsigned int)4);
    ___a_obj_archive_format_name_buf[___a_obj_archive_format_name_len] = '\0';
    __a_obj.archive_format_name = ___a_obj_archive_format_name_buf;
    __CPROVER_assume(__a_obj.file_count >= 0 && __a_obj.file_count <= (long)(4));
    __CPROVER_assume(__a_obj.archive_error_number >= 0 && __a_obj.archive_error_number <= (long)(4));
    char ___a_obj_error_buf[5];
    unsigned int ___a_obj_error_len;
    __CPROVER_assume(___a_obj_error_len <= (unsigned int)4);
    ___a_obj_error_buf[___a_obj_error_len] = '\0';
    __a_obj.error = ___a_obj_error_buf;
    char ___a_obj_current_code_buf[5];
    unsigned int ___a_obj_current_code_len;
    __CPROVER_assume(___a_obj_current_code_len <= (unsigned int)4);
    ___a_obj_current_code_buf[___a_obj_current_code_len] = '\0';
    __a_obj.current_code = ___a_obj_current_code_buf;
    char ___a_obj_read_data_block_buf[5];
    unsigned int ___a_obj_read_data_block_len;
    __CPROVER_assume(___a_obj_read_data_block_len <= (unsigned int)4);
    ___a_obj_read_data_block_buf[___a_obj_read_data_block_len] = '\0';
    __a_obj.read_data_block = ___a_obj_read_data_block_buf;
    __CPROVER_assume(__a_obj.read_data_offset >= 0 && __a_obj.read_data_offset <= (long)(4));
    __CPROVER_assume(__a_obj.read_data_output_offset >= 0 && __a_obj.read_data_output_offset <= (long)(4));
    __CPROVER_assume(__a_obj.read_data_remaining >= 0 && __a_obj.read_data_remaining <= (long)(4));
    __CPROVER_assume(__a_obj.read_data_is_posix_read >= 0 && __a_obj.read_data_is_posix_read <= (long)(4));
    /* opaque struct archive_entry: nondet pointer (archive_entry body not in TU) */
    struct archive_entry* entry;
    /* Step 2: precondition assumptions */
    /* precondition: true — no assumptions needed */
    /* Step 3: call function under test */
    int result = archive_match_excluded(_a, entry);
    /* Step 4: postcondition assertions */
    /* precondition: true — no assumptions needed */
    (void)result;
    return 0;
}
