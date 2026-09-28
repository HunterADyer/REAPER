/*
 * type_test_21_mutual_recursion.c — ADVERSAIRAL: mutually recursive structure
 * types (struct A holds a pointer to B, B holds a pointer to A) accessed in
 * mutually recursive functions. The detector must not infinite-loop on
 * recursive types OR recursive control flow, and must NOT collapse A/B into
 * one struct: they are distinct types that legitimately reference each other
 * with DIFFERENT field layouts.
 *
 *   struct a_node { int32_t av; struct b_node *bb; };  // +0, +8
 *   struct b_node { int64_t bv; struct a_node *aa; };  // +0, +8
 * NOTE: both have a 64-bit pointer at +8 but DIFFERENT first-field size
 * (4 vs 8) — access widths must NOT be the deciding "merge" excuse when the
 * types are genuinely different. Verdict may split (preferred) or merge, but
 * must not fabricate one struct mixing av (int32) and bv (int64) semantics.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct b_node;

struct a_node {
    int32_t av;              /* +0 4B */
    struct b_node *bb;       /* +8 */
};

struct b_node {
    int64_t bv;              /* +0 8B */
    struct a_node *aa;       /* +8 */
};

static volatile uint64_t g_sink;

NOINLINE
uint64_t b_sum(struct b_node *b, int depth);

NOINLINE
uint64_t a_sum(struct a_node *a, int depth) {
    if (!a || depth == 0)
        return 0;
    g_sink = (uint32_t)a->av;                /* +0 */
    return b_sum(a->bb, depth - 1) + (uint32_t)a->av;
}

NOINLINE
uint64_t b_sum(struct b_node *b, int depth) {
    if (!b || depth == 0)
        return 0;
    g_sink = (uint64_t)b->bv;                /* +0 (different size!) */
    return a_sum(b->aa, depth - 1) + b->bv;
}

int main(int argc, char **argv) {
    static struct a_node a;
    static struct b_node b;
    a.av = 3; a.bb = &b;
    b.bv = 40; b.aa = &a;
    g_sink = a_sum(&a, argc > 0 ? 2 : 0);
    return (int)(g_sink & 1);
}
