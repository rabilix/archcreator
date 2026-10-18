#include "alpha.h"
#include "signals.h"

/* A project wrapper around an accessor: the accessor is not a macro instantiation of its own. */
#define WRITE_SHARED(v) SP_Set_Shared(v)
/* Same name as beta's HELPER, different body: must not make alpha a writer of Temp. */
#define HELPER() SP_Get_Orphan()

/* Producer of Speed and Shared, consumer of Temp and Orphan (nobody in the App writes Orphan). */
void alpha_main(void)
{
    int t = SP_Get_Temp();
    if (HELPER() > t)
    {
        SP_Set_Speed(t * 2);
        SP_Set_Speed_Errorflag();
    }
    WRITE_SHARED(SP_SIGNALID_Speed);
}

/* Exported under a prefix alias (a_): the design knows it as alpha_helper. */
int a_helper(void)
{
    return 1;
}
