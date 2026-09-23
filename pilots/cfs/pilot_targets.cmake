######################################################################
#
# Weaver cFS pilot: target configuration
#
# Copied over sample_defs/targets.cmake by pilots/cfs/setup.sh to form
# pilot_defs/.  The mission is cFE, OSAL and the PSP with sample_app and
# sample_lib on one native CPU: the program sample_app actually runs in.
# (The lab apps' table definitions depend on other apps in the stock
# mission, so they are left out rather than dragging those in.)
#
######################################################################

SET(MISSION_NAME "WeaverPilot")
SET(SPACECRAFT_ID 0x42)

list(APPEND MISSION_GLOBAL_APPLIST sample_app sample_lib)

SET(FT_INSTALL_SUBDIR "host/functional-test")

SET(MISSION_CPUNAMES cpu1)

SET(cpu1_PROCESSORID 1)
SET(cpu1_APPLIST)
