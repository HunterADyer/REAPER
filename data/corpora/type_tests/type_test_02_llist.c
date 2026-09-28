/*
 * type_test_02_llist.c — shared linked-list node type across three
 * functions (find / insert / remove). Classic cross-function same-type:
 * every function takes "struct node*" and walks ->next/->val/->key.
 *
 * Ground truth to recover: struct lnode { void *key; struct lnode *next;
 * uint64_t val; }.
 *
 * Signature: an LLM/reverser must merge find/insert/remove into ONE node
 * type (the same pointer value flows between them via list head -> next).
 * No symbols: everything is stripped.
 */
#include <stdint.h>
#include <stddef.h>

struct lnode {
    void *key;
    struct lnode *next;
    uint64_t val;
};

struct lnode *find_node(struct lnode *head, const void *key) {
    struct lnode *cur = head;
    while (cur) {
        if (cur->key == key)
            return cur;
        cur = cur->next;          /* offset 8 */
    }
    return 0;
}

void insert_node(struct lnode **head, struct lnode *n) {
    n->next = *head;               /* offset 8 write */
    *head = n;
}

uint64_t remove_value(struct lnode **head, const void *key) {
    struct lnode *cur = *head, *prev = 0;
    while (cur) {
        if (cur->key == key) {     /* offset 0 read */
            if (prev) prev->next = cur->next;
            else *head = cur->next;
            return cur->val;       /* offset 16 read */
        }
        prev = cur;
        cur = cur->next;
    }
    return 0;
}

int main(int argc, char **argv) {
    struct lnode a = { 0 }, b = { 0 };
    insert_node(&a.next, &b);
    if (find_node(&a, argv) == &b)
        return (int)remove_value(&a.next, argv);
    return 0;
}
