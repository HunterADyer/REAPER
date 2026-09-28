/*
 * type_test_06_canary_noise.c — ADVERSAIRAL: stack-canary / frame noise.
 *
 * The detector must NOT emit a candidate for the TLS stack-canary read
 * (fsbase + 0x28) or for the saved-return-address slot (__return_addr) —
 * these are universal false positives on x86-64 that appeared in every
 * earlier test binary. This binary forces heavy canary + frame usage.
 *
 * It ALSO contains one genuine struct (struct config) shared across two
 * functions, so the filter must suppress the noise WITHOUT suppressing the
 * real candidate. A correct detector emits exactly ONE candidate.
 */
#include <stdint.h>

#define NOINLINE __attribute__((noinline))

#include <stddef.h>

struct config {
    uint32_t magic;
    uint32_t version;
    uint64_t page_count;
    void *meta;             /* pointer field */
    char tag[8];
};

static volatile uint64_t g_sink;

NOINLINE
static uint32_t cfg_validate(struct config *c) {
    /* forces the compiler to keep a real param; reads offset 0 and 8 */
    if (c->magic != 0xCAFE && c->version == 0)
        return 0;
    g_sink = (uint64_t)c->page_count;
    return c->magic ^ c->version;
}

NOINLINE
static void cfg_setup(struct config *c, uint64_t npages) {
    /* writes offsets 0, 8, 16 — a second, distinct view of the SAME type */
    c->page_count = npages;
    c->meta = (void *)(uintptr_t)npages;
    c->tag[7] = (char)npages;
    g_sink = (uint64_t)(uintptr_t)c->meta;
    (void)cfg_validate(c);
}

int main(int argc, char **argv) {
    /* big local so the frame pointer + stack protector are definitely used */
    char stack_buf[256];
    struct config local_cfg;
    local_cfg.magic = 0xCAFE;
    local_cfg.version = 7;
    for (int i = 0; i < 256; i++)
        stack_buf[i] = (char)(i * 3);
    cfg_setup(&local_cfg, (uint64_t)(size_t)stack_buf);
    return cfg_validate(&local_cfg) & 1;
}
