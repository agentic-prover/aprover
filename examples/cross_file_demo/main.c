/* main.c — system entry point in a separate compilation unit */
typedef int (*op_fn)(int);

/* Forward declarations (normally from a header) */
int apply_op(op_fn fn, int x);
int scaled_apply(op_fn fn, int x, int scale);

/* True system entry — no callers above it */
void system_entry(op_fn fn, int x) {
    int r = apply_op(fn, x);
    (void)r;
}
