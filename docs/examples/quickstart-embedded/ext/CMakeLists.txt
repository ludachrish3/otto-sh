# The Zephyr application that holds the extension. Built only by ext/build.sh,
# into build/ext/; the root CMakeLists.txt never references it.
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(quickstart_embedded_ext)

# The base application is a throwaway shell for the extension target.
target_sources(app PRIVATE src/main.c)

add_llext_target(mathops
  OUTPUT  ${ZEPHYR_BINARY_DIR}/mathops.llext
  SOURCES ${PROJECT_SOURCE_DIR}/src/ext.c)

# ext.c includes the runtime's sources from the patched embedded-gcov checkout
# that build.sh names.
if(NOT DEFINED EMBEDDED_GCOV_DIR)
  message(FATAL_ERROR "EMBEDDED_GCOV_DIR is not set; build through ext/build.sh")
endif()
target_include_directories(mathops_llext_lib PRIVATE ${EMBEDDED_GCOV_DIR}/code)

# Instrument the extension only (-O0 keeps the line table predictable).
target_compile_options(mathops_llext_lib PRIVATE -fprofile-arcs -ftest-coverage -O0)

# add_llext_target() is not part of the default build; tie it to the app.
add_dependencies(app mathops)
