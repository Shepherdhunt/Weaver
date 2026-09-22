#include <stdio.h>
#include "alias.h"
#include "edge.h"
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
    return 0;
}
