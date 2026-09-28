/*
 * type_test_05_false_merge.c — the over-merge trap.
 *
 * A generic helper handle_any(void *p) inspects bytes that LOOK like the
 * same field layout in two DIFFERENT structs. The detector sees offset
 * accesses at the SAME offsets and may union-find them into one candidate;
 * the VERDICT must recognize that these are TWO unrelated types passed to
 * one generic helper (reject the merge) rather than one struct.
 *
 * Ground truth: TWO distinct structs:
 *   struct widget { uint32_t id; uint32_t size; };
 *   struct gadget { uint32_t len;  uint32_t kind; };
 * Both start with two uint32s at offsets 0 and 4, accessed via a common
 * void* parameter. The offsets overlap but the types are unrelated.
 *
 * This binary exists to tune the caution bias: WHEN IN DOUBT, the verdict
 * may accept (over-merge is recoverable later) — but a good verdict that
 * sees widget->len/id semantics vs gadget->kind usage should reject one of
 * the two... at minimum the merged field NAME must not mix widget semantics
 * into gadget (id/size vs len/kind). Hand-tuning target.
 */
#include <stdint.h>
#include <stddef.h>

struct widget { uint32_t id; uint32_t size; };
struct gadget { uint32_t len; uint32_t kind; };

static volatile uint64_t g_sink;

/* generic helper: reads first TWO dwords through void* (offset 0 and 4) */
static uint64_t handle_any(const void *p) {
    const uint32_t *u = (const uint32_t *)p;
    return (uint64_t)u[0] << 32 | u[1];   /* accesses +0 and +4 */
}

uint32_t use_widget(struct widget *w) {
    return handle_any(w) & (w->id);
}

uint32_t use_gadget(struct gadget *g) {
    return (g->kind) & (uint32_t)(handle_any(g) >> 32);
}

uint32_t dispatch(int kind, void *p) {
    if (kind)
        return use_widget((struct widget *)p);
    return use_gadget((struct gadget *)p);
}

int main(int argc, char **argv) {
    struct widget w = { 5, 9 };
    struct gadget g = { 3, 2 };
    return (int)(dispatch(1, &w) + dispatch(0, &g));
}
