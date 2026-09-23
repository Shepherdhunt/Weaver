# Weaver cFS pilot: cFE ES startup script.
# Copied over sample_defs/generate_startup.cmake by pilots/cfs/setup.sh so the modules the
# loader resolves by name are exactly the program declared in weaver.yaml ("programs").
function (generate_cfs_startup_script CFS_INSTALL_DIR)
    set (STARTUP_FILE "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/${CFS_INSTALL_DIR}/cfe_es_startup.scr")
    file (WRITE ${STARTUP_FILE}
        "CFE_LIB, cfe_assert,  CFE_Assert_LibInit, ASSERT_LIB,    0,   0,     0x0, 0;\n"
        "CFE_LIB, sample_lib,  SAMPLE_LIB_Init,    SAMPLE_LIB,    0,   0,     0x0, 0;\n"
        "CFE_APP, sample_app,  SAMPLE_APP_Main,    SAMPLE_APP,   50,   32768, 0x0, 0;\n"
    )
endfunction(generate_cfs_startup_script)
