/*
 * type_test_07_false_merge_regs.c — ADVERSAIRAL: over-merge via registers.
 *
 * Two UNRELATED structs (header_a vs header_b) with IDENTICAL first fields
 * (offset 0 = uint32 magic, offset 8 = pointer). A generic
 * `void* copy12(void *dst, const void *src)` helper reads/writes the first
 * 12 bytes of BOTH. Binja will type the helper param as a pointer but NOT
 * as either struct, so both bases look alike (offset 0 + offset 8) and the
 * detector's call-context merge + same-type bridging will want to fuse them.
 *
 * The LLM VERDICT must reject the merge (or, at minimum, not fabricate a
 * name mixing header_a semantics with header_b). Hand-tuning target for the
 * caution bias.
 */
#include <stdint.h>

#define NOINLINE __attribute__((noinline))

#include <stddef.h>

struct header_a {
    uint32_t magic;
    uint32_t kind;
    void *payload;      /* offset 8 */
};

struct header_b {
    uint32_t magic;
    uint32_t flags;
    void *context;      /* offset 8 */
};

/* generic raw-copy helper: touches offset 0 and offset 8 on BOTH types */
NOINLINE
static void copy12(void *dst, const void *src) {
    uint64_t *d = (uint64_t *)dst;
    const uint64_t *s = (const uint64_t *)src;
    d[0] = s[0];        /* offset 0 */
    d[1] = s[1];        /* offset 8 */
}

NOINLINE
static uint32_t process_a(struct header_a *h) {
    struct header_a tmp;
    copy12(&tmp, h);
    return tmp.kind & h->magic;
}

NOINLINE
static uint32_t process_b(struct header_b *h) {
    struct header_b tmp;
    copy12(&tmp, h);
    return tmp.flags & h->magic;
}

int main(int argc, char **argv) {
    struct header_a a = { 0x1111, 1, (void *)0x1010 };
    struct header_b b = { 0x2222, 2, (void *)0x2020 };
    return (int)(process_a(&a) + process_b(&b));
}
