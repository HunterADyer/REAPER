/*
 * type_test_04_union.c — a genuine UNION + a struct, mixed in one binary.
 *
 * Ground truth to recover:
 *   union u_dispatch { uint64_t raw; struct { uint16_t lo; uint32_t mid; }; };
 *   struct packet { union u_dispatch d; uint8_t len; };
 *
 * The same byte range is accessed BOTH as a uint64 and as its component
 * halves (overlapping offsets -> union hint MUST fire). The packet struct
 * is passed across functions. A correct verdict emits kind="union" for u
 * and kind="struct" for packet.
 */
#include <stdint.h>

#define NOINLINE __attribute__((noinline))

#include <stddef.h>

union u_dispatch {
    uint64_t raw;
    struct {
        uint16_t lo;
        uint32_t mid;
    };
};

struct packet {
    union u_dispatch d;
    uint8_t len;
};

static volatile uint64_t g_sink;

NOINLINE
static uint16_t packet_lo(struct packet *p) {
    return p->d.lo;                 /* offset 0, 2 bytes — aliases raw low */
}

NOINLINE
static uint32_t packet_mid(struct packet *p) {
    return p->d.mid;                /* offset 4, 4 bytes — aliases raw mid */
}

NOINLINE
static uint64_t packet_raw(struct packet *p) {
    return p->d.raw;                /* offset 0, 8 bytes — FULL overlap */
}

NOINLINE
static void fill_packet(struct packet *p, uint64_t raw) {
    p->d.raw = raw;
    p->len = 4;                     /* offset 8 */
}

int process_packet(struct packet *p) {
    g_sink = packet_raw(p) ^ (uint64_t)packet_mid(p) ^ packet_lo(p);
    fill_packet(p, g_sink);
    return (int)(g_sink & 1);
}

int main(int argc, char **argv) {
    struct packet p = { 0 };
    return process_packet(&p);
}
