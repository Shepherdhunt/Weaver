#include <stdlib.h>
#include "params.h"

int p_counter;

/* Eligible: a read-only scalar input, read on every path. */
int p_scale(const int *factor, int x)
{
    return x * *factor;
}

/* Eligible (both parameters): non-const pointers that are only read. */
long p_sum2(long *a, long *b)
{
    return *a + *b + (long)sizeof(*a);
}

static void p_touch(int *q)
{
    *q += 1;
}

/* Blocked by flow evidence: a callee writes the target through another pointer. */
int p_read_after_touch(const int *v, int *w)
{
    p_touch(w);
    return *v;
}

static void p_bump(void)
{
    p_counter++;
}

/* Blocked: the target is written by name during the call. */
int p_snapshot(const int *c)
{
    p_bump();
    return *c;
}

/* Blocked: the pointer is null-tested. */
int p_maybe(const int *m)
{
    return m ? *m : -1;
}

/* Blocked: the target is read only on some paths. */
int p_cond(int c, const int *v)
{
    if (c)
        return *v;
    return 0;
}

/* Blocked: written through (an output parameter). */
void p_set(int *out)
{
    *out = 7;
}

/* Blocked: the function's address is taken. */
int p_cb(const int *v)
{
    return *v + 1;
}

int (*p_hook)(const int *v) = p_cb;

/* Blocked at a call site: another argument has side effects. */
int p_pair(const int *a, int b)
{
    return *a + b;
}

/* Unresolved without a reviewed effect model for rand(). */
int p_noisy(const int *v)
{
    int r = rand() % 1;
    return *v + r;
}

/* Eligible: callers pass a pointer value, not an address expression. */
int p_via_ptr(const int *v)
{
    return *v * 2;
}
