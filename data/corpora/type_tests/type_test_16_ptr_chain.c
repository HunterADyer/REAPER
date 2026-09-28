/*
 * type_test_16_ptr_chain.c — ADVERSAIRAL: pointer-to-pointer-to-pointer
 * chains. A triple indirection (char*** / entry**) where the STRUCT is at the
 * far end, and a helper operates on struct** .
 *
 *   struct item { uint32_t id; struct item *next; };
 *   chains tested:
 *     struct item*   normal pointer
 *     struct item**  pointer-to-pointer (e.g. output param / table cell)
 *     struct item*** triple (e.g. registry-of-handles)
 * The detector must NOT flatten the chain into garbage offsets: the field
 * access is on the LEAF object either way, but the base variable may be a
 * double pointer whose first deref hands us the real struct.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct item {
    uint32_t id;              /* +0 */
    struct item *next;        /* +8 */
};

static volatile uint64_t g_sink;

NOINLINE
uint32_t leaf_id(struct item *it) {
    return it->id;            /* +0 */
}

NOINLINE
uint32_t via_pp(struct item **pp) {
    return (*pp)->id;         /* **pp -> +0 */
}

NOINLINE
uint32_t via_ppp(struct item ***ppp) {
    return (*(*ppp))->id;     /* ***ppp -> +0 */
}

NOINLINE
uint32_t chain_id(struct item ***ppp) {
    return via_ppp(ppp) + via_pp(*ppp) + leaf_id(**ppp);
}

int main(int argc, char **argv) {
    static struct item a = { 7, 0 };
    struct item *p = &a;
    struct item **pp = &p;
    struct item ***ppp = &pp;
    g_sink = chain_id(ppp) ^ (uint32_t)argc;
    return (int)(g_sink & 1);
}
