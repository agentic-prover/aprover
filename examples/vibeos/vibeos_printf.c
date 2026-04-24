/*
 * VibeOS Printf — self-contained for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/printf.c
 * Changes: stubbed klog_putc, uart_putc, console_putc; removed kernel headers.
 *
 * Key properties CBMC checks:
 *  - print_num: buf[20] never overflows (at most 20 hex digits for uint64_t)
 *  - vprintf_internal: width parsing loop terminates
 *  - sprintf/snprintf: ctx.pos stays within [0, max)
 */

#include <stdint.h>
#include <stddef.h>
#include <stdarg.h>

/* --- stubs for output sinks --- */
static char klog_ring[256];
static int  klog_pos = 0;

static void klog_putc(char c) {
    klog_ring[klog_pos % 256] = c;
    klog_pos++;
}

static void uart_putc(char c)    { (void)c; }
static void console_putc(char c) { (void)c; }

/* --- printf implementation (unmodified logic) --- */

typedef void (*putchar_fn)(char c, void *ctx);

typedef struct {
    char *buf;
    int pos;
    int max;
} sprintf_ctx_t;

static void printf_putchar(char c, void *ctx) {
    (void)ctx;
    klog_putc(c);
    console_putc(c);
}

static void sprintf_putchar(char c, void *ctx) {
    sprintf_ctx_t *s = (sprintf_ctx_t *)ctx;
    if (s->pos < s->max - 1) {
        s->buf[s->pos++] = c;
    }
}

static int local_strlen(const char *s) {
    int len = 0;
    while (s[len]) len++;
    return len;
}

static int print_num(putchar_fn put, void *ctx, uint64_t num, int base, int width, int pad_zero, int uppercase) {
    char buf[20];
    const char *digits = uppercase ? "0123456789ABCDEF" : "0123456789abcdef";
    int i = 0;
    int count = 0;

    if (num == 0) {
        buf[i++] = '0';
    } else {
        while (num > 0) {
            buf[i++] = digits[num % base];
            num /= base;
        }
    }

    int pad = width - i;
    if (pad > 0 && pad_zero) {
        while (pad-- > 0) {
            put('0', ctx);
            count++;
        }
    } else if (pad > 0) {
        while (pad-- > 0) {
            put(' ', ctx);
            count++;
        }
    }

    while (i > 0) {
        put(buf[--i], ctx);
        count++;
    }

    return count;
}

static int print_signed(putchar_fn put, void *ctx, int64_t num, int width, int pad_zero) {
    int count = 0;

    if (num < 0) {
        put('-', ctx);
        count++;
        num = -num;
        width--;
    }

    count += print_num(put, ctx, (uint64_t)num, 10, width, pad_zero, 0);
    return count;
}

static int vprintf_internal(putchar_fn put, void *ctx, const char *fmt, va_list args) {
    int count = 0;

    while (*fmt) {
        if (*fmt != '%') {
            put(*fmt++, ctx);
            count++;
            continue;
        }

        fmt++;

        int pad_zero = 0;
        int left_justify = 0;

        while (*fmt == '0' || *fmt == '-') {
            if (*fmt == '0') pad_zero = 1;
            if (*fmt == '-') left_justify = 1;
            fmt++;
        }

        int width = 0;
        while (*fmt >= '0' && *fmt <= '9') {
            width = width * 10 + (*fmt - '0');
            fmt++;
        }

        int is_long = 0;
        if (*fmt == 'l') {
            is_long = 1;
            fmt++;
            if (*fmt == 'l') {
                fmt++;
            }
        }

        switch (*fmt) {
            case 'd':
            case 'i': {
                int64_t val = is_long ? va_arg(args, int64_t) : va_arg(args, int);
                count += print_signed(put, ctx, val, width, pad_zero);
                break;
            }
            case 'u': {
                uint64_t val = is_long ? va_arg(args, uint64_t) : va_arg(args, unsigned int);
                count += print_num(put, ctx, val, 10, width, pad_zero, 0);
                break;
            }
            case 'x': {
                uint64_t val = is_long ? va_arg(args, uint64_t) : va_arg(args, unsigned int);
                count += print_num(put, ctx, val, 16, width, pad_zero, 0);
                break;
            }
            case 'X': {
                uint64_t val = is_long ? va_arg(args, uint64_t) : va_arg(args, unsigned int);
                count += print_num(put, ctx, val, 16, width, pad_zero, 1);
                break;
            }
            case 'p': {
                uint64_t val = (uint64_t)va_arg(args, void *);
                put('0', ctx);
                put('x', ctx);
                count += 2;
                count += print_num(put, ctx, val, 16, 16, 1, 0);
                break;
            }
            case 's': {
                const char *s = va_arg(args, const char *);
                if (!s) s = "(null)";
                int len = local_strlen(s);
                int pad = width - len;

                if (!left_justify && pad > 0) {
                    while (pad-- > 0) {
                        put(' ', ctx);
                        count++;
                    }
                }
                while (*s) {
                    put(*s++, ctx);
                    count++;
                }
                if (left_justify && pad > 0) {
                    while (pad-- > 0) {
                        put(' ', ctx);
                        count++;
                    }
                }
                break;
            }
            case 'c': {
                char c = (char)va_arg(args, int);
                put(c, ctx);
                count++;
                break;
            }
            case '%':
                put('%', ctx);
                count++;
                break;
            default:
                put('%', ctx);
                put(*fmt, ctx);
                count += 2;
                break;
        }
        fmt++;
    }

    return count;
}

int sprintf(char *buf, const char *fmt, ...) {
    sprintf_ctx_t ctx = { buf, 0, 0x7FFFFFFF };
    va_list args;
    va_start(args, fmt);
    int count = vprintf_internal(sprintf_putchar, &ctx, fmt, args);
    va_end(args);
    buf[ctx.pos] = '\0';
    return count;
}

int snprintf(char *buf, int size, const char *fmt, ...) {
    sprintf_ctx_t ctx = { buf, 0, size };
    va_list args;
    va_start(args, fmt);
    int count = vprintf_internal(sprintf_putchar, &ctx, fmt, args);
    va_end(args);
    if (size > 0) {
        buf[ctx.pos] = '\0';
    }
    return count;
}
