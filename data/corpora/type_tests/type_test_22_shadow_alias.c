/*
 * type_test_22_shadow_alias.c — ADVERSAIRAL: the same backing buffer viewed BOTH
 * as a byte/word array AND as a struct, from different functions (manual
 * type-punning — no C union declared). This is the SAME-BYTE-RANGE aliasing
 * the detector's overlap_hint tries to catch, but WITHOUT a union keyword.
 *
 *   unsigned char pool[16];
 *   read as: uint32[4]        (function A: +0,+4,+8,+12 as 4B)
 *         or: struct { uint64 w; uint64 v; }  (function B: +0, +8 as 8B)
 *
 * Desired verdict: because +0 is read as BOTH 4B and 8B, +4 as 4B and part of
 * +0's 8B... this SHOULD be flagged as aliasing. A good reverser would model
 * it as a union or as one struct whose first field overlaps. The key check:
 * overlap_hint MUST fire (different sizes at the same offset).
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct words2_t {
    uint64_t w;              /* +0, 8B */
    uint64_t v;              /* +8, 8B */
};

static unsigned char pool[16] __attribute__((aligned(8)));
static volatile uint64_t g_sink;

NOINLINE
uint64_t via_u32(unsigned char *p) {
    const uint32_t *u = (const uint32_t *)p;
    return (uint64_t)u[0] + u[1] + u[2] + u[3];   /* +0,+4,+8,+12 as 4B each */
}

NOINLINE
uint64_t via_struct(struct words2_t *s) {
    return s->w + s->v;                            /* +0, +8 as 8B each */
}

NOINLINE
uint64_t shadow_read(void) {
    return via_u32(pool) ^ via_struct((struct words2_t *)pool);
}

int main(int argc, char **argv) {
    uint32_t *u = (uint32_t *)pool;
    for (int i = 0; i < 4; i++) u[i] = (uint32_t)(i + argc);
    g_sink = shadow_read();
    return (int)(g_sink & 1);
}
