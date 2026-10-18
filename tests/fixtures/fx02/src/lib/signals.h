#ifndef SIGNALS_H
#define SIGNALS_H
#include "signalpool.h"
/* Generated signal-pool accessors, one triple per signal. */
#define SP_Get_Speed()             sp_get(1)
#define SP_Set_Speed(x)            sp_set(1, (x))
#define SP_Test_Speed()            sp_test(1)
#define SP_Set_Speed_Errorflag()   sp_set(101, 1)
#define SP_Test_Speed_Errorflag()  sp_test(101)
#define SP_Get_Temp()              sp_get(2)
#define SP_Set_Temp(x)             sp_set(2, (x))
#define SP_Get_Orphan()            sp_get(3)
#define SP_Set_Orphan(x)           sp_set(3, (x))
#define SP_Get_Shared()            sp_get(4)
#define SP_Set_Shared(x)           sp_set(4, (x))
#define SP_SIGNALID_Speed 1
#endif
