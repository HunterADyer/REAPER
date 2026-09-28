/*
 * type_test_19_sparse_field.c — ADVERSAIRAL: a struct where ONLY ONE field is
 * ever accessed (across every function), and that field is at a NONZERO
 * offset. This defeats the "drop pure-indirection / offset-0-only" heuristic:
 * the base must be KEPT (nonzero offset) but there is still only one distinct
 * offset — a verdict that requires >=2 offsets to be "a struct" would wrongly
 * reject a real, if thin, type.
 *
 *   struct handle { uint8_t pad[8]; uint32_t cookie; };   // cookie at +8
 *   Only +8 is ever touched. Desired: detector surfaces +8; verdict MAY
 *   accept a 1-field struct (or reject "too sparse") — but the detector must
 *   NOT drop the candidate merely because it is single-offset (offset != 0).
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct handle {
    uint8_t pad[8];        /* +0 .. +7, never touched */
    uint32_t cookie;       /* +8, the ONLY accessed field */
};

static volatile uint32_t g_sink;

NOINLINE
uint32_t get_cookie(struct handle *h) {
    return h->cookie;                       /* +8 */
}

NOINLINE
void set_cookie(struct handle *h, uint32_t v) {
    h->cookie = v;                          /* +8 */
}

NOINLINE
uint32_t make_cookie(struct handle *h, uint32_t seed) {
    set_cookie(h, seed ^ 0xA5A5);
    return get_cookie(h);
}

int main(int argc, char **argv) {
    struct handle h;
    g_sink = make_cookie(&h, (uint32_t)argc);
    return (int)g_sink & 1;
}
