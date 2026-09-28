/*
 * type_test_17_by_value.c — ADVERSAIRAL: struct (de)composed by VALUE, not
 * through a pointer. On the SysV AMD64 ABI a small struct is passed in
 * registers — Binja then sees the fields as separate register params, with NO
 * single struct base pointer. Worse: a struct RETURNED by value may be
 * materialized through a HIDDEN pointer param the compiler synthesizes.
 *
 *   struct point { int32_t x, y; };   // 8 bytes, passed in one register pair
 *
 * The perversion: field accesses happen through register-typed bases (x1,
 * y0 ...) rather than a unified "point" pointer. A detector that only keys on
 * pointer+offset will see scattered +0 accesses; the verdict must NOT
 * fabricate a struct from unrelated register shuffling. Desired behavior:
 * either a single point candidate (if Binja keeps the aggregate) or NO
 * candidate (safe). Must never emit a nonsense multi-offset "struct".
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct point { int32_t x, y; };

static volatile int32_t g_sink;

NOINLINE
int32_t point_manhattan(struct point p) {
    /* passed BY VALUE: fields live in registers, not behind a pointer */
    int32_t ax = p.x < 0 ? -p.x : p.x;
    int32_t ay = p.y < 0 ? -p.y : p.y;
    return ax + ay;
}

NOINLINE
struct point point_make(int32_t x, int32_t y) {
    /* returned BY VALUE (may be behind a hidden pointer) */
    struct point p = { x, y };
    return p;
}

int main(int argc, char **argv) {
    struct point q = point_make(argc, argc - 3);
    g_sink = point_manhattan(q);
    return g_sink & 1;
}
