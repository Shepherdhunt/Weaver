#include "util.h"

static int *last_seen;

void util_touch(int *p)
{
    last_seen = p;
}

int util_seen(void)
{
    return last_seen ? *last_seen : -1;
}
