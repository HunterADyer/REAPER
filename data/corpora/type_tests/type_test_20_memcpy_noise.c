/*
 * type_test_20_memcpy_noise.c — ADVERSAIRAL: heavy memcpy/memset/intrinsic
 * noise around ONE real struct. Compiler emits INTRINSIC/memcpy/memset
 * nodes and 16-byte copy bursts that a structure detector must SKIP (they are
 * opaque bulk copies, not field accesses), while still catching the genuine
 * per-field reads/writes elsewhere.
 *
 *   struct blob { uint32_t magic; uint64_t size; char data[64]; };
 * Copied with memcpy in one function (noise) and field-accessed in another
 * (the real signal: +0, +8). Desired: ONE candidate from the field accesses;
 * the memcpy bursts must NOT generate fake overlapping-size "unions".
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define NOINLINE __attribute__((noinline))

struct blob {
    uint32_t magic;       /* +0 */
    uint64_t size;        /* +8 */
    char data[64];        /* +16 */
};

static volatile uint64_t g_sink;

NOINLINE
void blob_copy(struct blob *dst, const struct blob *src) {
    memcpy(dst, src, sizeof(struct blob));   /* intrinsic noise */
}

NOINLINE
uint64_t blob_touch(struct blob *b) {
    b->magic ^= 0x1234;                      /* +0 */
    b->size += 1;                            /* +8 */
    return b->magic + b->size;
}

int main(int argc, char **argv) {
    static struct blob src, dst;
    src.magic = 7; src.size = 100;
    memset(&dst, 0, sizeof(dst));            /* noise */
    blob_copy(&dst, &src);
    g_sink = blob_touch(&dst) ^ (uint32_t)argc;
    return (int)(g_sink & 1);
}
