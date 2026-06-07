# Compile the agent into the application image only when the gate is on.
# Guarding here (rather than relying on the module always linking) keeps the
# `no_fs` / 2.7 configs that don't enable the agent completely untouched.
if(CONFIG_OTTO_SNMP_AGENT)
  zephyr_library()
  zephyr_library_sources(src/snmp_agent.c)
endif()
