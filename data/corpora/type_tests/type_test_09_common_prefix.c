/*
 * type_test_09_common_prefix.c — ADVERSAIRAL: two structs sharing an IDENTICAL
 * leading layout (a "common prefix", like C inheritance / tagged headers).
 *
 *   struct net_hdr   { uint32_t magic; uint16_t proto; uint16_t flags; };
 *   struct udp_dgram { uint32_t magic; uint16_t proto; uint16_t flags;
 *                      uint16_t sport; uint16_t dport; };
 *
 * A generic function touches ONLY the common prefix (offsets 0, 4, 6) of both.
 * The detector will want to MERGE them into one candidate (same-type bridging
 * on the pointer, shared offsets + call-flow). The LLM verdict must decide:
 * these ARE the same leading layout but DIFFERENT types. Hand-tuning target —
 * a careful reverser would recover them as two structs (or one with the
 * shared prefix), but NEVER fabricate a single flat struct that renames
 * udp-specific fields onto a bare header.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct net_hdr {
    uint32_t magic;
    uint16_t proto;
    uint16_t flags;
};

struct udp_dgram {
    uint32_t magic;
    uint16_t proto;
    uint16_t flags;
    uint16_t sport;
    uint16_t dport;
};

static volatile uint64_t g_sink;

NOINLINE
static uint32_t prefix_check(const void *p) {
    const uint32_t *magic = (const uint32_t *)p;   /* +0 */
    const uint16_t *proto = (const uint16_t *)((const char *)p + 4); /* +4 */
    const uint16_t *flags = (const uint16_t *)((const char *)p + 6); /* +6 */
    return (*magic & 0xff) | ((uint32_t)*proto << 8) | ((uint32_t)*flags << 16);
}

NOINLINE
uint32_t handle_net(struct net_hdr *h, uint32_t extra) {
    return prefix_check(h) ^ extra ^ h->proto;
}

NOINLINE
uint32_t handle_udp(struct udp_dgram *d, uint32_t extra) {
    return prefix_check(d) ^ extra ^ d->dport;   /* touches dport at +10 too */
}

NOINLINE
uint32_t dispatch(int kind, void *p, uint32_t extra) {
    if (kind)
        return handle_net((struct net_hdr *)p, extra);
    return handle_udp((struct udp_dgram *)p, extra);
}

int main(int argc, char **argv) {
    struct net_hdr h = { 0xCAFE, 1, 2 };
    struct udp_dgram d = { 0xCAFE, 3, 4, 5, 6 };
    g_sink = dispatch(argc & 1, (void *)&h, 0x1111);
    g_sink ^= dispatch(0, (void *)&d, (uint32_t)argc);
    return (int)(g_sink & 1);
}
