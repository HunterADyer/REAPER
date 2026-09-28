/*
 * type_test_01_nested.c — nested structs + pointer fields, shared type
 * across THREE functions that genuinely pass the same struct pointer around.
 *
 * Ground truth to recover:
 *   struct inner_item { uint32_t id; uint32_t tag; };
 *   struct record { struct inner_item *item; uint16_t flags; char *label; };
 *
 * - parse_record() takes the record, passes it to validate_record() and
 *   summarize_record(). All three REACH INTO the same struct fields, so
 *   call-context sharing evidence legitimately spans three functions.
 * - Fields are accessed at DIFFERENT offsets in each function on purpose:
 *   any single function only sees a PARTIAL view; together they are complete.
 * - The binary is stripped, so no symbol names help.
 */
#include <stdint.h>

#define NOINLINE __attribute__((noinline))

#include <stddef.h>

struct inner_item {
    uint32_t id;
    uint32_t tag;
};

struct record {
    struct inner_item *item;
    uint16_t flags;
    char *label;
};

static volatile uint64_t g_sink;

/* partial view #1: touches gs->item / item->id */
NOINLINE
static int parse_item(struct inner_item *it) {
    if (it == 0)
        return 0;
    return (int)(it->id);
}

/* partial view #2: touches record->item->tag, record->flags */
NOINLINE
static int validate_record(struct record *rec) {
    if (!rec || !rec->item)
        return -1;
    uint32_t t = rec->item->tag;
    return (t & rec->flags) ? 1 : 0;
}

/* partial view #3: touches record->label, record->flags */
NOINLINE
static void summarize_record(struct record *rec) {
    g_sink = (uint64_t)(uintptr_t)rec->label ^ rec->flags;
}

int parse_record(struct record *rec) {
    if (!rec)
        return -1;
    int a = parse_item(rec->item);      /* passes the SAME item pointer */
    int b = validate_record(rec);        /* passes the SAME record pointer */
    summarize_record(rec);               /* and again */
    return a + b;
}

int main(int argc, char **argv) {
    struct record r = { 0 };
    struct inner_item it = { 7, 3 };
    r.item = &it;
    r.flags = 3;
    r.label = "hello";
    return parse_record(&r) & 1;
}
