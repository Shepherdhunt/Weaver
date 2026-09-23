#include <stdio.h>
#include "alias.h"
#include "edge.h"
#include "params.h"
#include "util.h"

int main(void)
{
    int x = 0;
    struct pair pr = {1, 2};
    printf("basic=%u\n", la_basic());
    printf("struct=%d\n", la_struct());
    printf("global=%d\n", la_global());
    printf("global=%d\n", la_global());
    printf("escape=%d seen=%d\n", la_escape(), util_seen() >= -1);
    printf("reassign=%d,%d\n", la_reassign(0), la_reassign(1));
    printf("compare=%d\n", la_compare());
    printf("macro=%d\n", la_macro());
    printf("shadow=%d\n", la_shadow());
    printf("inactive=%d\n", la_inactive());
    printf("config=%d\n", la_config());
    printf("volatile=%d\n", la_volatile());
    update(&x, &x);
    printf("update=%d\n", x);
    printf("member=%d element=%d const=%d\n", e_member(), e_element(), e_const_view(4));
    printf("through=%d subscript=%d multi=%d for=%d\n", e_through_pointer(&pr), e_subscript(), e_multi(),
           e_for_init());
    printf("goto=%d,%d\n", e_goto(0), e_goto(1));
    x = e_cleanup();
    printf("cleanup=%d after=%d\n", x, e_cleanup_target);
    {
        int k = 3, w = 5;
        long la = 4, lb = 6;
        int *kp = &k;
        printf("scale=%d sum2=%ld\n", p_scale(&k, 7), p_sum2(&la, &lb));
        int t1 = p_read_after_touch(&w, &w);
        int t2 = p_read_after_touch(&k, &w);
        int s1 = p_snapshot(&p_counter);
        printf("touch=%d,%d\n", t1, t2);
        printf("snapshot=%d counter=%d\n", s1, p_counter);
        printf("maybe=%d,%d cond=%d,%d\n", p_maybe(&k), p_maybe(0), p_cond(1, &k), p_cond(0, 0));
        p_set(&w);
        printf("set=%d hook=%d\n", w, p_hook(&k));
        w = p_pair(&w, w++);
        t1 = p_noisy(&k);
        t2 = p_via_ptr(kp);
        printf("pair=%d noisy=%d via=%d\n", w, t1, t2);
    }
    return 0;
}
