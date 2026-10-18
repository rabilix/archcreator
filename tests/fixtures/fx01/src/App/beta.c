#include "beta.h"
#include "alpha.h"

extern int alpha_internal(void);

static int helper(int x)
{
    return x * 2;
}

int beta_api(int v)
{
    alpha_counter++;
    return helper(v) + alpha_internal();
}

void beta_api_cb(void)
{
}

int beta_hidden(void)
{
    return 7;
}

int beta_feature(void)
{
    return 1;
}

/* Reached only through the external Rte_Call_Beta_Op() macro: not an interface bypass. */
int beta_rte_op(void)
{
    return 5;
}
