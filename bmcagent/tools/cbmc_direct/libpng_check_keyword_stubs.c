/* Minimal no-op stubs for the warning symbols that png_check_keyword may
 * reference, so the leaf routine links without an initialized png_struct.
 * These are reached only on the truncation / bad-character paths and emit
 * diagnostics in real libpng; for buffer-safety analysis they are irrelevant.
 * Signatures mirror png.h / pngpriv.h exactly. */
#include "pngpriv.h"

#ifdef PNG_WARNINGS_SUPPORTED
void png_warning(const png_struct *png_ptr, const char *warning_message)
{ (void)png_ptr; (void)warning_message; }

void png_warning_parameter(png_warning_parameters p, int number,
    const char *string)
{ (void)p; (void)number; (void)string; }

void png_warning_parameter_signed(png_warning_parameters p, int number,
    int format, png_int_32 value)
{ (void)p; (void)number; (void)format; (void)value; }

void png_formatted_warning(const png_struct *png_ptr, png_warning_parameters p,
    const char *message)
{ (void)png_ptr; (void)p; (void)message; }
#endif
