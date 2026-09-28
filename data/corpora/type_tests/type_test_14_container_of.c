/*
 * type_test_14_container_of.c — ADVERSAIRAL: intrusive list via an embedded
 * struct member + negative offsets (container_of pattern).
 *
 *   struct list_head { struct list_head *next; struct list_head *prev; };
 *   struct item { uint32_t magic; struct list_head link; char name[16]; };
 *
 * Code receives ONLY a struct list_head* (the embedded "link") and recovers
 * the owner via container_of:  owner = (void*)((char*)link - offsetof(item,
 * link)) — i.e. field accesses NEGATIVE relative to the list_head base. A
 * detector that ignores or mangles negative offsets will miss the owner
 * fields; it must surface accesses at negative offsets (from the embedded
 * member) AND still see the list_head itself (+0/+8) as a legitimate (nested)
 * struct.
 */
#include <stdint.h>
#include <stddef.h>

#define NOINLINE __attribute__((noinline))

struct list_head {
    struct list_head *next;
    struct list_head *prev;
};

struct item {
    uint32_t magic;                       /* +0 */
    struct list_head link;                /* +8 */
    char name[16];                        /* +24 */
};

static volatile uint64_t g_sink;

NOINLINE
static struct item *to_item(struct list_head *l) {
    return (struct item *)((char *)l - offsetof(struct item, link));
}

NOINLINE
uint32_t walk_list(struct list_head *head, const char *wanted) {
    uint32_t hits = 0;
    for (struct list_head *l = head; l; l = l->next) {
        struct item *it = to_item(l);
        /* negative-offset access: magic is BEFORE the link member */
        if (it->magic != 0 && (wanted == 0 || it->name[0] == wanted[0]))
            hits++;
    }
    return hits;
}

NOINLINE
void push(struct list_head **head, struct item *it) {
    it->link.next = *head;                /* +8 (list_head) */
    it->link.prev = 0;
    *head = &it->link;
}

int main(int argc, char **argv) {
    static struct item a, b;
    static struct list_head head = { 0 };
    a.magic = 1; b.magic = 2;
    push(&head, &a);
    push(&head, &b);
    g_sink = walk_list(head.next, argv ? *argv : 0);
    return (int)(g_sink & 1);
}
