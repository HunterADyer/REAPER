/*
 * type_test_23_manual_iterator.c — ADVERSAIRAL: a struct walked one ENTRY at a
 * time via a manual raw-pointer advancing pattern (NOT the compiler's native
 * iterator, which would produce ARRAY_INDEX on a typed base). The accessor
 * never uses `p->field` on the row base — instead it dereferences relative
 * to a raw cursor, so the resulting HLIL has offsets relative to a MOVING
 * pointer. A
 * naive detector will mis-assign offsets (they shift as the cursor advances).
 *
 *   struct record { int64_t key; int64_t value; };   // 16 bytes
 *   walker: int64_t *cur = base;  cur += 2 per step.
 * The perversion: each logical step moves by 16 bytes, so a field read looks
 * like `*(cur + offset)` where cur is a raw int64* that is ALSO re-based each
 * iteration. Desired: detector recovers +0/+8 (key/value) and does NOT
 * hallucinate +16/+24 from the stepping.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct record {
    int64_t key;       /* +0 */
    int64_t value;     /* +8 */
};

static volatile uint64_t g_sink;

NOINLINE
int64_t manual_sum(int64_t *base, int n) {
    int64_t acc = 0;
    int64_t *cur = base;
    for (int i = 0; i < n; i++) {
        acc += cur[0];      /* +0 (key) */
        acc += cur[1];      /* +8 (value) */
        cur += 2;           /* advance one record */
    }
    return acc;
}

int main(int argc, char **argv) {
    static struct record r[4];
    for (int i = 0; i < 4; i++) { r[i].key = i; r[i].value = i * 10; }
    g_sink = manual_sum((int64_t *)r, argc > 0 ? 4 : 0);
    return (int)(g_sink & 1);
}
