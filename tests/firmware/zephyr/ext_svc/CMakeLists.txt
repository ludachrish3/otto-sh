# Compile the helpers into the application image only when the gate is on
# (same pattern as ../snmp_agent).
if(CONFIG_OTTO_EXT_SVC)
  zephyr_library()
  zephyr_library_sources(src/ext_svc.c)
endif()
