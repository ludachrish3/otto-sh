# The hierarchical coverage fixture: a shared library and two executables in
# their own object directories, plus a unit-test tree that compiles the
# library's sources again. Every object is instrumented.
cmake_minimum_required(VERSION 3.16)
project(otto_hier CXX)

option(STALE "Build the stale tree (different code, different stamps)" OFF)

add_compile_options(--coverage -O0 -g)
add_link_options(--coverage)
if(STALE)
  add_compile_definitions(STALE=1)
endif()

add_subdirectory(src/core)
add_subdirectory(src/app)
add_subdirectory(src/tool)
add_subdirectory(tests/unit)
