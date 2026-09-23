#ifndef DEMO_PARAMS_H
#define DEMO_PARAMS_H

extern int p_counter;
extern int (*p_hook)(const int *v);

int p_scale(const int *factor, int x);
long p_sum2(long *a, long *b);
int p_read_after_touch(const int *v, int *w);
int p_snapshot(const int *c);
int p_maybe(const int *m);
int p_cond(int c, const int *v);
void p_set(int *out);
int p_cb(const int *v);
int p_pair(const int *a, int b);
int p_noisy(const int *v);
int p_via_ptr(const int *v);

#endif
