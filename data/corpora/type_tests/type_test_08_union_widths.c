/*
 * type_test_08_union_widths.c — ADVERSAIRAL: union with genuinely DIFFERENT
 * access widths at the SAME offset.
 *
 * The detector's union overlap_hint only fires when it can measure REAL
 * access sizes. This binary accesses the same byte range BOTH as a 4-byte
 * int32 (offset 0) and as an 8-byte uint64 (offset 0) — if the detector
 * hard-codes 8 bytes for every access (the prior bug), it cannot tell these
 * alias and will miss the union. A fixed detector reports overlapping sizes
 * and the Linux kernel-style layout is recovered as a union.
 *
 *   union u_state {
 *       int32_t  word32;      // offset 0, 4 bytes
 *       uint64_t word64;      // offset 0, 8 bytes (aliases word32)
 *   };
 *   struct stateful {
 *       union u_state u;      // two access patterns at offset 0
 *       uint16_t mode;        // offset 8
 *   };
 */
#include <stdint.h>

#define NOINLINE __attribute__((noinline))

#include <stddef.h>

union u_state {
    int32_t word32;
    uint64_t word64;
};

struct stateful {
    union u_state u;
    uint16_t mode;
};

static volatile uint64_t g_sink;

NOINLINE
static int32_t state_read32(struct stateful *s) {
    return s->u.word32;          /* offset 0, 4-byte access */
}

NOINLINE
static uint64_t state_read64(struct stateful *s) {
    return s->u.word64;          /* offset 0, 8-byte access — FULL overlap */
}

NOINLINE
static void state_write64(struct stateful *s, uint64_t v) {
    s->u.word64 = v;             /* offset 0, 8-byte write */
    s->mode = (uint16_t)(v & 3); /* offset 8 */
}

int main(int argc, char **argv) {
    struct stateful s = { 0 };
    state_write64(&s, 0x1122334455667788ULL);
    g_sink = state_read32(&s) ^ (uint64_t)state_read64(&s);
    return (int)(g_sink & 1);
}
