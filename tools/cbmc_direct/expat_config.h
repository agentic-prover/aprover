/* Minimal hand-written expat_config.h for standalone CBMC/ASan leaf harnesses.
   Mirrors a typical Linux build. Only the knobs xmltok.c needs. */
#ifndef EXPAT_CONFIG_H
#define EXPAT_CONFIG_H

#define HAVE_STDINT_H 1
#define HAVE_STRING_H 1
#define HAVE_STDLIB_H 1
#define HAVE_MEMORY_H 1
#define STDC_HEADERS 1

#define XML_DTD 1
#define XML_NS 1
#define XML_GE 1

#define XML_CONTEXT_BYTES 1024

/* Little-endian Linux default */
#define BYTEORDER 1234

/* Entropy source: pick /dev/urandom so no special syscalls are needed.
   (Not actually exercised by the leaf conversion routines.) */
#define XML_DEV_URANDOM 1

#define PACKAGE_NAME "expat"
#define PACKAGE_STRING "expat 2.x"
#define PACKAGE_VERSION "2.x"

#endif /* EXPAT_CONFIG_H */
