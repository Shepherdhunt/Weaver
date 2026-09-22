#include "edge.h"

struct wrap { struct pair in; int arr[4]; };

/* Eligible: the target is reached through a member path. */
int e_member(void)
{
    struct wrap w = {{1, 2}, {0, 0, 0, 0}};
    int *ip = &w.in.b;
    *ip = 7;
    return w.in.a + w.in.b;
}

/* Eligible: the target is an array element with a constant index. */
int e_element(void)
{
    int arr[3] = {1, 2, 3};
    int *ep = &arr[1];
    *ep *= 10;
    return arr[0] + arr[1] + arr[2];
}

/* Eligible: a read-only view, including an unevaluated use. */
int e_const_view(int seed)
{
    int v = seed + 1;
    const int *vp = &v;
    return *vp + (int)sizeof(*vp);
}

/* Blocked: the target path goes through another pointer. */
int e_through_pointer(struct pair *pp)
{
    int *bp = &pp->b;
    *bp = 4;
    return pp->b;
}

/* Blocked: the pointer is indexed. */
int e_subscript(void)
{
    int k = 5;
    int *kp = &k;
    return kp[0];
}

/* Blocked: declared together with another declarator. */
int e_multi(void)
{
    int x = 1, y = 2;
    int *xp = &x, *yp = &y;
    *xp += *yp;
    return x;
}

/* Blocked: declared in a for-init clause. */
int e_for_init(void)
{
    int n = 0;
    for (int *np = &n; *np < 3; (*np)++)
        ;
    return n;
}

/* Blocked: a goto can reach a use while bypassing the initialization. */
int e_goto(int c)
{
    int g = 1;
    if (c)
        goto skip;
    int *gp = &g;
    g = 3;
skip:
    return c ? g : *gp;
}

/* Blocked: the cleanup attribute runs code with the pointer's address. */
int e_cleanup_target;

static void e_release(int **pp)
{
    **pp += 100;
}

int e_cleanup(void)
{
    int *cp __attribute__((cleanup(e_release))) = &e_cleanup_target;
    *cp = 1;
    return e_cleanup_target;
}
