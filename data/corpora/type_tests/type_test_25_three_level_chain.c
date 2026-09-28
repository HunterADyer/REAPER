/*
 * type_test_25_three_level_chain.c — ADVERSAIRAL: the SAME struct pointer
 * passed through a THREE-level dispatch chain (root -> layer -> leaf), where
 * each level only OBSERVES a DISJOINT subset of fields. This is the strongest
 * positive merge test: no single function sees more than 2 offsets, but the
 * transitive call-context must unify ALL THREE into one complete struct.
 *
 *   struct message { uint64_t magic; uint32_t kind; uint32_t seq;
 *                    void *payload; };
 *   - root  touches +0 (magic)
 *   - layer touches +8 (kind), +16 (seq)
 *   - leaf  touches +24 (payload), +0 (magic)
 * Desired: ONE candidate across all three functions with offsets
 * {0, 8, 16, 24} — the complete message struct — never three partial ones.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct message {
    uint64_t magic;    /* +0 */
    uint32_t kind;     /* +8 */
    uint32_t seq;      /* +16 */
    void *payload;     /* +24 */
};

static volatile uint64_t g_sink;

NOINLINE
uint32_t leaf_check(struct message *m) {
    g_sink = (uint64_t)(uintptr_t)m->payload;   /* +24 */
    return (m->magic == 0xCAFE) ? 1 : 0;        /* +0 */
}

NOINLINE
uint32_t layer_route(struct message *m) {
    uint32_t r = leaf_check(m);
    r ^= m->kind ^ m->seq;                       /* +8, +16 */
    return r;
}

NOINLINE
uint32_t root_send(struct message *m) {
    if (m->magic != 0xCAFE)                      /* +0 */
        return 0;
    return layer_route(m);
}

int main(int argc, char **argv) {
    struct message m = { 0xCAFE, 1, 2, (void *)(uintptr_t)0x1234 };
    g_sink = root_send(&m) ^ (uint32_t)argc;
    return (int)(g_sink & 1);
}
