# Weaver report: cfs-pilot (apps/sample_app)

Generated 2026-09-23T16:11:49+00:00 by Weaver 0.1.0 at commit `088b2fa828db`.

## Evidence

- Units analysed: 287 (5 in scope)
- Evidence status: secondary-checked 287
- SVF flow evidence (native): cpu1 complete, elf2cfetbl complete, cfeconfig_platformdata_tool complete, cfe_testcase.so complete (open), cfe_ts_crc complete, tlm_recv complete, cmd_send complete
- GCC IPA points-to (native): cpu1 complete, elf2cfetbl complete, cfeconfig_platformdata_tool complete, cfe_testcase.so complete, cfe_ts_crc complete, tlm_recv complete, cmd_send complete; analysis deviations from production flags: -Werror

## Pointers

15 pointer finding(s): 10 parameter, 5 local.
By what each does to its target: 8 escapes, 4 unused, 2 read-only, 1 reassigned.

## Recipes

| Recipe | Applicable | Eligible | Most common blockers | Would unlock alone |
|---|---|---|---|---|
| `local-alias` | 5 | 0 | LA.target-stable (5) | LA.target-stable (5) |
| `scalar-input` | 10 | 0 | SI.read-only-uses (10), SI.unconditional-read (10), SI.no-modification-during-call (10), SI.no-concurrent-writers (10) | - |

### Eligible (0)

None.

### Blocked (15)

| Pointer | Recipe | Blocking preconditions (first evidence) |
|---|---|---|
| `SBBufPtr` in `SAMPLE_APP_Main` (`apps/sample_app/fsw/src/sample_app.c:48`) | `local-alias` | ✗ LA.target-stable: no C initializer; the pointer is not bound once at its declaration |
| `Msg` in `SAMPLE_APP_SendHkCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:47`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const SAMPLE_APP_SendHkCmd_t' is not a scalar type<br>✗ SI.read-only-uses: the target is never read (an unused parameter needs a different recipe)<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_SendHkCmd() at apps/sample_app/fsw/src/sample_app_cmds.c:61: CFE_SB_TransmitMsg() may write framework-owned BufDscPtr, CFE_Assert_StatusReport, CFE_C |
| `Msg` in `SAMPLE_APP_NoopCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:79`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const SAMPLE_APP_NoopCmd_t' is not a scalar type<br>✗ SI.read-only-uses: the target is never read (an unused parameter needs a different recipe)<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_NoopCmd() at apps/sample_app/fsw/src/sample_app_cmds.c:83: CFE_EVS_SendEvent() may write framework-owned BufDscPtr, CFE_Assert_StatusReport, CFE_Conf |
| `Msg` in `SAMPLE_APP_ResetCountersCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:98`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const SAMPLE_APP_ResetCountersCmd_t' is not a scalar type<br>✗ SI.read-only-uses: the target is never read (an unused parameter needs a different recipe)<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_ResetCountersCmd() at apps/sample_app/fsw/src/sample_app_cmds.c:103: CFE_EVS_SendEvent() may write framework-owned BufDscPtr, CFE_Assert_StatusReport |
| `Msg` in `SAMPLE_APP_ProcessCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:114`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const SAMPLE_APP_ProcessCmd_t' is not a scalar type<br>✗ SI.read-only-uses: the target is never read (an unused parameter needs a different recipe)<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_ProcessCmd() at apps/sample_app/fsw/src/sample_app_cmds.c:123: CFE_TBL_GetAddress() may write framework-owned BufDscPtr, CFE_Assert_StatusReport, CFE |
| `TblAddr` in `SAMPLE_APP_ProcessCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:117`) | `local-alias` | ✗ LA.target-stable: no C initializer; the pointer is not bound once at its declaration |
| `TblPtr` in `SAMPLE_APP_ProcessCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:118`) | `local-alias` | ✗ LA.target-stable: no C initializer; the pointer is not bound once at its declaration |
| `TableName` in `SAMPLE_APP_ProcessCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:119`) | `local-alias` | ✗ LA.target-stable: initializer is a ImplicitCastExpr(ArrayToPointerDecay), not the address of an object |
| `Msg` in `SAMPLE_APP_DisplayParamCmd` (`apps/sample_app/fsw/src/sample_app_cmds.c:155`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const SAMPLE_APP_DisplayParamCmd_t' is not a scalar type<br>✗ SI.read-only-uses: line 161: arrow (member-read)<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_DisplayParamCmd() at apps/sample_app/fsw/src/sample_app_cmds.c:158: CFE_EVS_SendEvent() may write framework-owned BufDscPtr, CFE_Assert_StatusReport, |
| `MsgPtr` in `SAMPLE_APP_VerifyCmdLength` (`apps/sample_app/fsw/src/sample_app_dispatch.c:39`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const CFE_MSG_Message_t' is not a scalar type<br>✗ SI.read-only-uses: line 46: passed as argument 1 to CFE_MSG_GetSize()<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_VerifyCmdLength() at apps/sample_app/fsw/src/sample_app_dispatch.c:56: CFE_EVS_SendEvent() may write framework-owned BufDscPtr, CFE_Assert_StatusRepo |
| `SBBufPtr` in `SAMPLE_APP_ProcessGroundCommand` (`apps/sample_app/fsw/src/sample_app_dispatch.c:77`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const CFE_SB_Buffer_t' is not a scalar type<br>✗ SI.read-only-uses: line 81: arrow (address)<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_ProcessGroundCommand() at apps/sample_app/fsw/src/sample_app_dispatch.c:117: CFE_EVS_SendEvent() may write framework-owned BufDscPtr, CFE_Assert_Stat |
| `SBBufPtr` in `SAMPLE_APP_TaskPipe` (`apps/sample_app/fsw/src/sample_app_dispatch.c:132`) | `scalar-input` | ✗ SI.parameter-type: pointee 'const CFE_SB_Buffer_t' is not a scalar type<br>✗ SI.read-only-uses: line 146: arrow (address)<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>✗ SI.no-modification-during-call: SAMPLE_APP_TaskPipe() at apps/sample_app/fsw/src/sample_app_dispatch.c:162: CFE_EVS_SendEvent() may write framework-owned BufDscPtr, CFE_Assert_StatusReport, CF |
| `TblData` in `SAMPLE_APP_TblValidationFunc` (`apps/sample_app/fsw/src/sample_app_utils.c:37`) | `scalar-input` | ✗ SI.parameter-type: pointee 'void' is not a scalar type<br>✗ SI.read-only-uses: line 40: explicitly converted (BitCast) to SAMPLE_APP_ExampleTable_t *<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>? SI.no-modification-during-call: GCC (sample_app.so): argument 1 may point to memory outside the image, and the call writes memory [native/cpu1] |
| `TblDataPtr` in `SAMPLE_APP_TblValidationFunc` (`apps/sample_app/fsw/src/sample_app_utils.c:40`) | `local-alias` | ✗ LA.target-stable: initializer is a CStyleCastExpr(BitCast), not the address of an object |
| `TableName` in `SAMPLE_APP_GetCrc` (`apps/sample_app/fsw/src/sample_app_utils.c:59`) | `scalar-input` | ✗ SI.read-only-uses: line 65: passed as argument 2 to CFE_TBL_GetInfo()<br>✗ SI.unconditional-read: native: no read of the target is executed on every path from entry; reading at the call site would add a read the original program does not perform<br>? SI.no-modification-during-call: SAMPLE_APP_GetCrc() at apps/sample_app/fsw/src/sample_app_utils.c:65: CFE_TBL_GetInfo() writes through argument 1: points-to set includes unknown memory (native<br>? SI.no-concurrent-writers: native/cpu1: the parameter of SAMPLE_APP_GetCrc() may point to memory points-to analysis cannot identify |

## Contracts

- `C-11150a15` **held**: SBBufPtr in SAMPLE_APP_Main(): borrowed

## Reading this report

✗ marks a violated precondition (counter-evidence exists); ? marks one that could not be established from the available evidence. Unknown is never reported as safe. Eligibility is a proposal: each transaction is still validated in isolated workspaces before it can be accepted.

