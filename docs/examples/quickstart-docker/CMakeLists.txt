# The docker track's example: the shell track's text library and tools plus a
# third tool, built here as static binaries for small container images, and
# GoogleTest unit tests for the library.
cmake_minimum_required(VERSION 3.24)
project(quickstart_docker CXX)
set(CMAKE_CXX_STANDARD 17)

# GoogleTest: the copy installed on this machine when there is one
# (e.g. `apt install libgtest-dev`), else this pinned release, downloaded once.
include(FetchContent)
FetchContent_Declare(
  googletest
  URL https://github.com/google/googletest/releases/download/v1.15.2/googletest-1.15.2.tar.gz
  URL_HASH SHA256=7b42b4d6ed48810c5362c265a17faebe90dc2373c885e5216439d37927f02926
  FIND_PACKAGE_ARGS NAMES GTest
)
FetchContent_MakeAvailable(googletest)

# Coverage instruments this project's code only: these options come after
# GoogleTest, so its sources are built without them.
add_compile_options(--coverage -O0 -g)
add_link_options(--coverage)

enable_testing()
add_subdirectory(src/textlib)
add_subdirectory(src/wordcount)
add_subdirectory(src/shout)
add_subdirectory(src/banner)
add_subdirectory(tests/unit)

# The tools run in alpine containers, which have no glibc: link them statically.
# They are copied into the images, not built there, so build them on a machine of
# the docker host's architecture. The unit tests run here, so they link as usual.
target_link_options(wordcount PRIVATE -static)
target_link_options(shout PRIVATE -static)
target_link_options(banner PRIVATE -static)
