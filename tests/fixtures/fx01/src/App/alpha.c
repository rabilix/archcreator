#include "alpha.h"
#include "alpha_cfg.h"
#include "beta.h"
#include "gamma_helper.h"
#include "ext.h"
#include "rte_like.h"

/* Seeded INTERFACE-001 (outgoing): beta_hidden is not declared in beta.h. */
extern int beta_hidden(void);

int alpha_counter;
static void (*alpha_cb)(void);

/* Same name as the static helper in beta.c; must stay a separate symbol. */
static int helper(int x)
{
    return x + 1;
}

void alpha_init(void)
{
    alpha_counter = 0;
    alpha_cb = beta_api_cb;
    (void) Rte_Call_Beta_Op();
}

/* Seeded INTERFACE-001 (incoming): exported, used by beta, not declared in alpha.h. */
int alpha_internal(void)
{
    return alpha_counter;
}

/* Seeded COMPLEXITY-001: CCN 7 (lizard counts the #ifdef) and nesting 4 exceed the fixture limits (5 / 3). */
int alpha_step(int value)
{
    int r = helper(value);
    r += beta_api(value);
    r += beta_hidden();
    r += ext_value;
    if (value > GAMMA_MAX)
    {
        r = 0;
    }
    if (value > 1)
    {
        if (value > 2)
        {
            if (value > 3)
            {
                if (value > 4)
                {
                    r++;
                }
            }
        }
    }
    alpha_cb();
#ifdef FEATURE_X
    r += beta_feature();
#endif
    return r + ALPHA_CFG_MODE;
}

/* Seeded NAMING-001: exported without the alpha_ prefix. */
int wrongName(void)
{
    return ALPHA_LIMIT;
}
