#ifndef BETA_H
#define BETA_H
void beta_main(void);
/* Object-like wrapper with a parenthesised body: not a function-like macro. */
#define READ_SPEED (SP_Get_Speed())
/* A .c-local HELPER exists in alpha.c and in beta.c with different bodies: each file sees only its own. */
#endif
