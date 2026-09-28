/*
 * type_test_13_runtime_index.c — ADVERSAIRAL: struct array indexed by a
 * RUNTIME variable. `arr[i].field` is a genuine struct field access where the
 * ELEMENT size scales the index — the field offset is static but the base
 * pointer moves by i*sizeof(T).
 *
 * A naive detector that only honors CONSTANT offsets adds to the base will
 * see `*(arr + i*32 + 8)` and either (a) fabricate a nonsense offset or
 * (b) silently drop every access. Correct behavior: recognize the constant
 * part (the field offset at +8/+16) even under a SYMBOLIC index — i.e. do
 * NOT produce per-index garbage, and DO recover the row layout from the
 * constant displacement after factoring out the runtime index scaling.
 *
 *   struct row { uint32_t id; uint32_t next; uint64_t value; };
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct row {
    uint32_t id;
    uint32_t next;
    uint64_t value;
};

static volatile uint64_t g_sink;

NOINLINE
uint64_t row_sum(struct row *rows, int n, uint32_t wanted) {
    uint64_t acc = 0;
    for (int i = 0; i < n; i++) {
        if (rows[i].id == wanted)          /* *(rows + i*sizeof + 0) */
            acc += rows[i].value;          /* *(rows + i*sizeof + 16) */
        acc += rows[i].next;               /* *(rows + i*sizeof + 4) */
    }
    return acc;
}

NOINLINE
uint32_t row_id_at(struct row *rows, int i) {
    return rows[i].id;                      /* runtime index, +0 field */
}

int main(int argc, char **argv) {
    static struct row rows[8];
    for (int i = 0; i < 8; i++) {
        rows[i].id = (uint32_t)(i * 3);
        rows[i].next = (uint32_t)(i + 1);
        rows[i].value = (uint64_t)i * 1000;
    }
    g_sink = row_sum(rows, (argc > 0) ? 8 : 0, (uint32_t)argc);
    g_sink += row_id_at(rows, argc & 7);
    return (int)(g_sink & 1);
}
