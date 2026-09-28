/*
 * type_test_10_tagged_union.c — ADVERSAIRAL: a VARIANT / tagged union where a
 * discriminator field at +0 selects DIFFERENT interpretations of the SAME
 * payload bytes at +8.
 *
 *   struct variant {
 *       uint64_t tag;          // +0
 *       union {                // +8 .. +24 aliased
 *           struct { int64_t a; int64_t b; } pair;
 *           struct { char name[8]; uint32_t len; } str;
 *       } payload;
 *   };
 *
 * The detector sees offset +8 accessed as int64/int64 (pair) in some
 * functions AND as char[8]/uint32 (str) in others — a genuine union at
 * +8, but INDEPENDENT of the tag (the tag is never overlapped). A correct
 * verdict must emit struct variant { uint64_t tag; union u { ... } payload; }
 * — and must NOT mistake the +0 tag for part of the union.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct variant {
    uint64_t tag;              /* +0 discriminator (not part of any union) */
    union {
        struct {
            int64_t a;         /* +8 */
            int64_t b;         /* +16 */
        } pair;
        struct {
            char name[8];      /* +8 */
            uint32_t len;      /* +16 */
        } str;
    } payload;
};

static volatile uint64_t g_sink;

NOINLINE
static int64_t read_pair(struct variant *v) {
    return v->payload.pair.a + v->payload.pair.b;   /* +8, +16 as int64 */
}

NOINLINE
static uint32_t read_str_len(struct variant *v) {
    return v->payload.str.len;                       /* +16 as uint32 */
}

NOINLINE
static uint64_t read_tag(struct variant *v) {
    return v->tag;                                   /* +0, separate field */
}

NOINLINE
uint64_t process(struct variant *v) {
    g_sink = read_tag(v);
    if (v->tag == 1)
        return g_sink ^ (uint64_t)read_pair(v);
    return g_sink ^ (uint64_t)read_str_len(v);
}

int main(int argc, char **argv) {
    struct variant v;
    v.tag = (uint64_t)(argc & 1);
    v.payload.str.len = (uint32_t)argc;
    return (int)(process(&v) & 1);
}
