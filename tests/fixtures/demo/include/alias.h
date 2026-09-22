#ifndef DEMO_ALIAS_H
#define DEMO_ALIAS_H

extern int g_counter;
unsigned la_basic(void);
int la_struct(void);
int la_global(void);
int la_escape(void);
int la_reassign(int c);
int la_compare(void);
int la_macro(void);
int la_shadow(void);
int la_inactive(void);
int la_config(void);
int la_volatile(void);
void update(int *a, int *b);

#endif
