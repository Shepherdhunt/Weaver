#ifndef DEMO_EDGE_H
#define DEMO_EDGE_H

struct pair { int a; int b; };

extern int e_cleanup_target;
int e_member(void);
int e_element(void);
int e_const_view(int seed);
int e_through_pointer(struct pair *pp);
int e_subscript(void);
int e_multi(void);
int e_for_init(void);
int e_goto(int c);
int e_cleanup(void);

#endif
