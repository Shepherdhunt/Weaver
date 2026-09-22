#include "util.h"
#include "alias.h"

#ifndef SCALE
#define SCALE 1
#endif
#define DEREF(q) (*(q))

int g_counter;
struct point { int x; int y; };

/* Eligible: local alias of one known object (plan example). */
unsigned la_basic(void)
{
    unsigned total = 3;
    unsigned *p = &total;
    *p += 2;
    return total;
}

/* Eligible: alias of a struct, used through '->'. */
int la_struct(void)
{
    struct point pt = {1, 2};
    struct point *pp = &pt;
    pp->x = 5;
    return pp->x + pt.y;
}

/* Eligible: alias of a global object. */
int la_global(void)
{
    int *gp = &g_counter;
    (*gp)++;
    return g_counter;
}

/* Blocked: the address escapes to a callee. */
int la_escape(void)
{
    int v = 1;
    int *ep = &v;
    util_touch(ep);
    return *ep;
}

/* Blocked: the pointer is reassigned. */
int la_reassign(int c)
{
    int a = 1, b = 2;
    int *rp = &a;
    if (c)
        rp = &b;
    *rp = 7;
    return a + b;
}

/* Blocked: pointer identity is observed. */
int la_compare(void)
{
    int a = 1;
    int *cp = &a;
    int *dp = &a;
    return cp == dp;
}

/* Blocked: a use is inside a macro expansion. */
int la_macro(void)
{
    int m = 4;
    int *mp = &m;
    return DEREF(mp);
}

/* Blocked: the target's name is shadowed at a use site. */
int la_shadow(void)
{
    int s = 1;
    int *sp = &s;
    {
        int s = 10;
        *sp += s;
    }
    return s;
}

/* Blocked: a reference sits in code no configuration compiles. */
int la_inactive(void)
{
    int q = 1;
    int *qp = &q;
#ifdef NEVER_DEFINED
    util_touch(qp);
#endif
    *qp = 3;
    return q;
}

/* Configuration-dependent: with -DTRACE the pointer escapes. */
int la_config(void)
{
    int t = 1;
    int *tp = &t;
#ifdef TRACE
    util_touch(tp);
#endif
    *tp = SCALE;
    return t;
}

/* Blocked: a volatile access would become a non-volatile one. */
int la_volatile(void)
{
    int w = 0;
    volatile int *wp = &w;
    *wp = 1;
    return w;
}

/* Aliased parameters (plan rejection case for output-to-return). */
void update(int *a, int *b)
{
    *a = 1;
    *b += 1;
}
