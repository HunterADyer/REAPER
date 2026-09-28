/*
 * type_test_24_wide_vector.c — ADVERSAIRAL: 16/32-byte WIDE accesses.
 * Compilers (esp. -O2/-O3) coalesce adjacent struct members into 128-bit /
 * 256-bit loads (`v[pair]` = 16B, SSE/AVX). A detector measuring "real access
 * size" from the Deref width now sees SIZE 16 accesses — it must handle sizes
 * >8B and must NOT treat two adjacent 8-byte members coalesced into one 16B
 * read as a bizarre union (it's just a wider load across two real fields).
 *
 *   struct pair { uint64_t lo; uint64_t hi; };   // +0, +8
 *   pair_read() does `__int128 t = *(__int128*)p;` — one 16B access +0.
 * Desired: overlap_hint should NOT fire here (a single 16B read is one
 * contiguous access, not overlapping sizes); the struct should still be
 * recoverable from any +8 accesses elsewhere.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct pair {
    uint64_t lo;       /* +0 */
    uint64_t hi;       /* +8 */
};

static volatile uint64_t g_sink;

NOINLINE
uint64_t pair_load(struct pair *p) {
    /* one 16-byte access that covers BOTH fields — wide-vector idiom */
    __int128 t = *(__int128 *)p;
    return (uint64_t)t ^ (uint64_t)(t >> 64);
}

NOINLINE
uint64_t pair_hi(struct pair *p) {
    return p->hi;      /* +8, standard narrow access */
}

NOINLINE
uint64_t pair_probe(struct pair *p) {
    return pair_hi(p) ^ pair_load(p);
}

int main(int argc, char **argv) {
    struct pair p = { (uint64_t)argc, (uint64_t)argc + 1 };
    g_sink = pair_probe(&p);
    return (int)(g_sink & 1);
}
