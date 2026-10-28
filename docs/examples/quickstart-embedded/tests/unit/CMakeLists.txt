# Unit tests for the math helpers. mathops.c includes no libc or Zephyr header,
# so the source the extension is built from compiles here unchanged.
add_executable(unit_tests test_mathops.cpp ${PROJECT_SOURCE_DIR}/logic/mathops.c)
target_include_directories(unit_tests PRIVATE ${PROJECT_SOURCE_DIR}/logic)
target_link_libraries(unit_tests PRIVATE GTest::gtest_main)

# Register each TEST with CTest by listing the binary's tests after the build:
# a binary that holds no tests registers none, and `ctest --no-tests=error` fails.
include(GoogleTest)
gtest_discover_tests(unit_tests)
