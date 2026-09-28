/*
 * type_test_15_callback.c — ADVERSAIRAL: struct is shared ONLY through
 * INDIRECT calls (function-pointer callbacks). The detector cannot resolve
 * call targets for `(*cb)(p)`, so NO call-context edge is produced; merging
 * across the handler functions must rely on identical Binja type bridging
 * (or identical offset sets) alone. This tests that the verdict still
 * unifies the partial views when the only сходство is the structure itself.
 *
 *   struct session { uint32_t id; void *buf; uint32_t len; };
 *   callbacks: start_session/end_session both derive from void(*)(void*).
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct session {
    uint32_t id;              /* +0 */
    void *buf;                /* +8 */
    uint32_t len;             /* +16 */
};

typedef void (*cb_t)(void *);

static volatile uint32_t g_sink;

NOINLINE
static void cb_start(void *p) {
    struct session *s = (struct session *)p;
    s->buf = (void *)(uintptr_t)(s->buf);   /* +8 */
    s->len = 0;                              /* +16 */
    g_sink = s->id;                          /* +0 */
}

NOINLINE
static void cb_end(void *p) {
    struct session *s = (struct session *)p;
    g_sink = s->len ^ s->id;                 /* +16, +0 */
    s->buf = 0;                              /* +8 ... */
}

NOINLINE
void run_callbacks(cb_t *cbs, void *sessions[], int n) {
    for (int i = 0; i < n; i++)
        if (cbs[i])
            cbs[i](sessions[i]);
}

int main(int argc, char **argv) {
    static struct session s[2];
    cb_t cbs[2] = { cb_start, cb_end };
    s[0].id = 10; s[1].id = 20;
    s[0].buf = (void *)1; s[1].buf = (void *)2;
    run_callbacks(cbs, (void **)s, argc > 0 ? 2 : 0);
    return (int)g_sink & 1;
}
