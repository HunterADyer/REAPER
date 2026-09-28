/*
 * type_test_12_bitfields.c — ADVERSAIRAL: bitfields packed into a single
 * dword. The CPU-level accesses are to ONE 4-byte word at +0, regardless of
 * which bitfield is read/written.
 *
 *   struct status { uint32_t :4; uint32_t ready:1; uint32_t err:1;
 *                  uint32_t code:8; uint32_t :18; };   // 4 bytes total
 *
 * A detector that keys on overlapping offsets-with-different-sizes would see
 * EVERY bitfield op as "+0 size 4" and could fabricate an impossible union —
 * but they all alias on the SAME 4-byte word with the SAME size, so this is
 * ONE field, NOT a union. The perversion: many accesses at the SAME offset
 * with the SAME size should collapse cleanly; anything else is a bug signal.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct status {
    uint32_t :4;
    uint32_t ready : 1;
    uint32_t err : 1;
    uint32_t code : 8;
    uint32_t : 18;
};

static volatile uint32_t g_sink;

NOINLINE
uint32_t bit_poll(struct status *s) {
    /* several independent reads of the SAME (+0, 4-byte) word */
    return s->ready | (s->err << 1) | (s->code << 2);
}

NOINLINE
void bit_set(struct status *s, uint32_t code) {
    s->code = code;            /* +0 4B write */
    s->ready = 1;              /* +0 4B write */
}

int main(int argc, char **argv) {
    struct status st;
    bit_set(&st, (uint32_t)(argc & 0xff));
    g_sink = bit_poll(&st);
    return (int)(g_sink & 1);
}
