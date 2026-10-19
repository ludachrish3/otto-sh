cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(cov_ext_app)

# Host app is a throwaway shell for the LLEXT target (see src/main.c).
target_sources(app PRIVATE src/main.c)

# The coverage extension: product code + embedded-gcov runtime in one TU
# (Approach A — the base stays stock). Built as a relocatable LLEXT object.
add_llext_target(cov_ext
  OUTPUT  ${ZEPHYR_BINARY_DIR}/cov_ext.llext
  SOURCES ${PROJECT_SOURCE_DIR}/src/cov_ext.c)

# cov_ext.c #includes the embedded-gcov sources from the *patched* submodule.
# One canonical copy — no vendoring. See README for the submodule init + patch.
target_include_directories(cov_ext_llext_lib PRIVATE
  ${PROJECT_SOURCE_DIR}/../third_party/embedded-gcov/code)

# Instrument only the extension TU (-O0 keeps the line table predictable).
target_compile_options(cov_ext_llext_lib PRIVATE -fprofile-arcs -ftest-coverage -O0)

# add_llext_target() is not part of the default build; tie it to the app so a
# plain `west build` produces cov_ext.llext.
add_dependencies(app cov_ext)
