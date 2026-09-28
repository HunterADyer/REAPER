/*
 * type_test_03_pointers.c — heavy POINTER usage: pointer-to-pointer,
 * pointer arithmetic over a table of pointers, and a char** argv style
 * array. Tests that the detector handles pointer chains (double derefs)
 * and that the LLM names pointer-holding structs without flattening them.
 *
 * Ground truth to recover:
 *   struct table_entry { char *name; uint64_t value; };
 *   and a pointer-to-pointer table (struct table_entry **).
 *
 * Functions: build_table() then lookup_row() — the table pointer flows
 * between them AND into per-row entries. Row entries are NOT all the same
 * object, but the TYPE is shared: a good detector/verdict treats rows as
 * the same struct, NOT three per-row structs.
 */
#include <stdint.h>
#include <stddef.h>

struct table_entry {
    char *name;
    uint64_t value;
};

static volatile uint64_t g_sink;

static uint64_t *row_value(struct table_entry *row) {
    return &row->value;                    /* offset 8 */
}

static char *row_name(struct table_entry *row) {
    return row->name;                      /* offset 0 */
}

int lookup_row(struct table_entry **table, int n, const char *wanted) {
    uint64_t acc = 0;
    for (int i = 0; i < n; i++) {
        struct table_entry *row = table[i];   /* pointer-to-pointer deref */
        if (row_name(row) == wanted) {
            acc += *row_value(row);
        }
    }
    return (int)(acc & 1);
}

int build_table(struct table_entry **table, int n) {
    for (int i = 0; i < n; i++) {
        table[i]->name = (char *)(uintptr_t)i;   /* offset 0 write */
        table[i]->value = (uint64_t)i;           /* offset 8 write */
    }
    return lookup_row(table, n, (const char *)(uintptr_t)1);
}

int main(int argc, char **argv) {
    static struct table_entry rows[4];
    struct table_entry *table[4];
    for (int i = 0; i < 4; i++) table[i] = &rows[i];
    return build_table(table, 4);
}
