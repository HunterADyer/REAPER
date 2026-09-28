/*
 * type_test_18_global_base.c — ADVERSAIRAL: struct fields accessed through a
 * GLOBAL base (a fixed .bss/.data address), shared across functions. There is
 * no stack/param base variable — the access is `*(&g_cfg + 0x8)` with the base
 * being a global symbol (g_cfg). Binja commonly renders this via a data
 * variable whose "name" is the symbol or a temp. The detector must still
 * capture the offsets and, crucially, MUST merge the SAME global base across
 * the functions that touch different fields (one struct, not per-function).
 *
 *   struct global_config { uint32_t magic; uint32_t version; uint64_t flag;
 *                          void *hook; };
 *   Global instance: g_cfg.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct global_config {
    uint32_t magic;      /* +0 */
    uint32_t version;    /* +4 */
    uint64_t flag;       /* +8 */
    void *hook;          /* +16 */
};

static struct global_config g_cfg = { 0, 0, 0, 0 };
static volatile uint64_t g_sink;

NOINLINE
uint32_t cfg_read_version(void) {
    return g_cfg.version;                 /* &g_cfg + 4 */
}

NOINLINE
void cfg_bump(void) {
    g_cfg.flag += 1;                       /* &g_cfg + 8 */
    g_cfg.magic = 0xFEED;                  /* &g_cfg + 0 */
}

NOINLINE
void *cfg_get_hook(void) {
    return g_cfg.hook;                     /* &g_cfg + 16 */
}

int main(int argc, char **argv) {
    g_cfg.hook = argv ? (void *)(uintptr_t)(argc + 1) : 0;
    cfg_bump();
    g_sink = cfg_read_version() ^ (uint64_t)cfg_get_hook() ^ g_cfg.flag;
    return (int)(g_sink & 1);
}
