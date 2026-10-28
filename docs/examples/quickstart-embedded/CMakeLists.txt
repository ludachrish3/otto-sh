# The embedded track's example: GoogleTest unit tests for the extension's math
# helpers, built for this machine with coverage. The extension itself is built
# by Zephyr (ext/build.sh), not by this CMake tree.
cmake_minimum_required(VERSION 3.24)
project(quickstart_embedded C CXX)
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
add_subdirectory(tests/unit)
