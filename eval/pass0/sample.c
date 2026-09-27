/*
 * eval/pass0/sample.c — small purpose-built Pass-0 (type-recovery) corpus.
 *
 * Deliberately exercises REAPER's Phase 2→4 type-recovery path with a handful
 * of *distinct* struct layouts that are referenced by the binary's OWN code
 * from multiple functions, so the StructAccessDetector has real candidate
 * groups and metric 5 (type-recovery accuracy) is graded against a non-trivial
 * layout set — unlike eval/cjson, which is one giant cJSON struct.
 *
 *   struct cfg     — scalar variety + char[64] array + opaque data/callback ptr
 *   struct point   — plain geometry pair (nested BY VALUE inside struct box)
 *   struct box     — nested struct member + int fields (deliberate padding)
 *   struct gnode   — linked node: ptr + enum + ptr + int
 *   struct symtab  — pointer-to-pointer storage + counters
 *
 * Every accessor is EXTERNALLY linked and noinline so it remains a real,
 * addressable function in the stripped binary (metrics 1-4 need a non-trivial
 * denominator) and keeps explicit struct-field reads in the disassembly
 * (metric 5 needs the detector / recovery to have something to find). Calls
 * from main keep every function reachable — no dead code elimination.
 *
 * Build BOTH variants with ./build.sh:
 *   pass0_sample_symbols  — unstripped, -g (ground-truth source)
 *   pass0_sample          — STRIPPED (the REAPER input)
 * Then run `python3 extract_ground_truth.py` for the 8.1-shaped ground truth
 * (DWARF via pyelftools — no Binary Ninja required).
 */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#define NOINLINE __attribute__((noinline))

/* (1) configuration block — scalar variety + char array + opaque pointers. */
struct cfg {
    int32_t   version;      /*  0 */
    uint16_t  mode;         /*  4 */
    uint8_t   verbosity;    /*  6 */
    uint8_t   pad;          /*  7 */
    char      logfile[64];  /*  8 */
    void     *user;         /* 72 */
    int      (*on_event)(struct cfg *, int); /* 80 */
    double    threshold;    /* 88 */
};

/* (2) nested geometry pair — 16 bytes, no padding. */
struct point {
    double x;               /*  0 */
    double y;               /*  8 */
};

/* (2b) box embeds point BY VALUE — exercises nested struct member layout. */
struct box {
    struct point min;       /*  0 */
    struct point max;       /* 16 */
    uint32_t     color;     /* 32 */
    int32_t      layer;     /* 36 */
};

/* (3) graph node — pointer + enum + pointer + int. */
enum nkind { NK_ENTRY, NK_BLOCK, NK_EDGE, NK_EXIT };
struct gnode {
    struct gnode *next;     /*  0 */
    uint32_t      id;       /*  8 */
    enum nkind    kind;     /* 12 */
    const char   *label;    /* 16 */
    int           weight;   /* 24 */
};

/* (4) symbol table — pointer-to-pointer storage + counters. */
struct symtab {
    struct gnode **slots;   /*  0 */
    uint32_t       slot_count; /*  8 */
    uint32_t       used;    /* 12 */
    uint64_t       generation; /* 16 */
};

/* ---- cfg accessors (cross-function reads of the same struct) ---- */
NOINLINE int cfg_version(const struct cfg *c) {
    return c->version + c->verbosity;
}

NOINLINE void cfg_dump(const struct cfg *c) {
    unsigned i, n = 0;
    for (i = 0; i < 64 && c->logfile[i]; i++)
        n++;
    printf("%s mode=%u n=%u user=%p thr=%.1f\n",
           c->logfile, c->mode, n, c->user, c->threshold);
}

NOINLINE int cfg_emit(struct cfg *c, int ev) {
    if (!c->on_event)
        return 0;
    return c->on_event(c, ev) + c->version;
}

/* ---- box / point accessors ---- */
NOINLINE double box_area(const struct box *b) {
    double w = b->max.x - b->min.x;
    double h = b->max.y - b->min.y;
    return (w < 0 ? -w : w) * (h < 0 ? -h : h);
}

NOINLINE int box_hit(const struct box *b, double px, double py) {
    if (px < b->min.x || px > b->max.x)
        return 0;
    if (py < b->min.y || py > b->max.y)
        return 0;
    return b->layer > 0 ? 1 : 0;
}

NOINLINE double pt_dist2(const struct point *p) {
    return p->x * p->x + p->y * p->y;
}

/* ---- gnode / symtab accessors ---- */
NOINLINE const char *node_kind_name(const struct gnode *n) {
    switch (n->kind) {
        case NK_ENTRY: return "entry";
        case NK_BLOCK: return "block";
        case NK_EDGE:  return "edge";
        case NK_EXIT:  return "exit";
    }
    return "?";
}

NOINLINE uint64_t node_rank(const struct gnode *n) {
    uint64_t r = (uint64_t)n->id * 1000000007u;
    if (n->next)
        r += n->next->id;
    if (n->label)
        r += (uint64_t)strlen(n->label);
    return r + (uint64_t)n->weight;
}

NOINLINE uint32_t symtab_load(const struct symtab *t) {
    return t->used <= t->slot_count ? t->used : t->slot_count;
}

NOINLINE uint64_t symtab_generation(const struct symtab *t) {
    uint64_t g = t->generation;
    uint32_t n = symtab_load(t), i;
    for (i = 0; i < n; i++) {
        const struct gnode *p = t->slots[i];
        if (p)
            g += p->id;
    }
    return g;
}

/* Used only via cfg.on_event (stays reachable through the function pointer). */
static int on_overflow(struct cfg *c, int ev) {
    return (int)c->threshold + ev;
}

int main(void) {
    struct cfg c = { 3, 2, 1, 0, "eval.log", NULL, on_overflow, 0.5 };
    struct box b;
    b.min.x = 0.0; b.min.y = 0.0;
    b.max.x = 4.0; b.max.y = 5.0;
    b.color = 0x112233u; b.layer = 1;

    struct gnode a   = { NULL, 1, NK_ENTRY, "root", 7 };
    struct gnode e1  = { &a, 2, NK_BLOCK,  NULL, 0 };
    struct gnode n[3] = {
        { NULL, 10, NK_EDGE,  "e",  3 },
        { NULL, 11, NK_BLOCK, NULL, 4 },
        { NULL, 12, NK_EXIT,  NULL, 5 },
    };
    struct gnode *slots[2] = { &n[0], &n[1] };
    struct symtab t = { slots, 2, 2, 9 };

    printf("cfg=%d area=%.1f hit=%d d2=%.1f rank=%llu load=%u gen=%llu "
           "kind=%s color=%u\n",
           cfg_version(&c) + cfg_emit(&c, 1),
           box_area(&b), box_hit(&b, 2.0, 2.0), pt_dist2(&b.min),
           (unsigned long long)(node_rank(&e1) ^ node_rank(&a)),
           symtab_load(&t), (unsigned long long)symtab_generation(&t),
           node_kind_name(&n[2]), b.color);
    cfg_dump(&c);
    return 0;
}
