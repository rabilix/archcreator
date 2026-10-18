#include "beta.h"
#include "signals.h"

#define HELPER() SP_Set_Temp(READ_SPEED / 2)

/* Consumer of Speed (value and error flag), producer of Temp; second writer of Shared. */
void beta_main(void)
{
    if (SP_Test_Speed() && !SP_Test_Speed_Errorflag())
    {
        HELPER();
    }
    SP_Set_Shared(0);
}
