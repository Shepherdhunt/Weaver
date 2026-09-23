# Weaver cFS pilot: cpu1 install customization.
# Copied over sample_defs/cpu1/install_custom.cmake by pilots/cfs/setup.sh; the stock file
# wires tables of apps this mission does not build (to_lab, sch_lab, lc, hs, ...).

if (${SIMULATION} MATCHES "^native")
    install(PROGRAMS ${CMAKE_CURRENT_LIST_DIR}/container-start DESTINATION cpu1)
endif()

add_cfe_tables(sample_app sample_app_alt1.c)

install(SCRIPT ${MISSION_DEFS}/generate_startup.cmake)
install(CODE "generate_cfs_startup_script(\"${TGTNAME}/${INSTALL_SUBDIR}\" ${${TGTNAME}_APPLIST})")
