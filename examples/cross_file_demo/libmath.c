/* libmath.c — low-level operation, called exclusively from main.c */
typedef int (*op_fn)(int);

/* Crash: fn can be NULL (caller's responsibility to check) */
int apply_op(op_fn fn, int x) {
    return fn(x);   /* pointer_dereference when fn == NULL */
}
