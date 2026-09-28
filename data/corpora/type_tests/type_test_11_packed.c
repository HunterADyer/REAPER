/*
 * type_test_11_packed.c — ADVERSAIRAL: __attribute__((packed)) struct with
 * UNALIGNED, non-natural offsets.
 *
 *   struct packed_record {
 *       uint8_t  kind;      // +0
 *       uint32_t seq;       // +1  (unaligned!)
 *       uint16_t port;      // +5  (unaligned!)
 *       uint64_t payload;   // +7  (unaligned!)
 *   } __attribute__((packed));
 *
 * Offsets are 0,1,5,7 with sizes 1,4,2,8 — NOT natural alignment. This breaks
 * detectors that assume offset arithmetic on natural boundaries, that derive
 * sizes by rounding, or that assume (offset % size == 0). A correct detector
 * reports each access at its TRUE unaligned offset/size. A correct verdict
 * recovers all four fields (single struct, NOT a union, NOT a flattened
 * byte-array).
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct __attribute__((packed)) packed_record {
    uint8_t  kind;
    uint32_t seq;
    uint16_t port;
    uint64_t payload;
};

static volatile uint64_t g_sink;

NOINLINE
uint64_t pack_read(const struct packed_record *r) {
    /* forces individual unaligned loads; compiler cannot fold via registers */
    return (uint64_t)r->kind
         | ((uint64_t)r->seq << 8)
         | ((uint64_t)r->port << 40)
         | (r->payload << 48);
}

NOINLINE
void pack_write(struct packed_record *r, uint64_t v, uint32_t seq) {
    r->kind = (uint8_t)v;          /* +0 1B */
    r->seq = seq;                  /* +1 4B */
    r->port = (uint16_t)(v >> 8);  /* +5 2B */
    r->payload = v ^ seq;          /* +7 8B */
}

int main(int argc, char **argv) {
    struct packed_record r = { 0 };
    pack_write(&r, (uint64_t)argc * 0x123456789ULL, (uint32_t)argc);
    g_sink = pack_read(&r);
    return (int)(g_sink & 1);
}
