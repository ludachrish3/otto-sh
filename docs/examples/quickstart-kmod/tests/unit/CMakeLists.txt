# Unit tests for the module's word counter. wordcount.c includes no kernel
# header, so the source the module is built from compiles here unchanged.
add_executable(unit_tests test_wordcount.cpp ${PROJECT_SOURCE_DIR}/module/wordcount.c)
target_include_directories(unit_tests PRIVATE ${PROJECT_SOURCE_DIR}/module)
target_link_libraries(unit_tests PRIVATE GTest::gtest_main)

# Register each TEST with CTest by listing the binary's tests after the build:
# a binary that holds no tests registers none, and `ctest --no-tests=error` fails.
include(GoogleTest)
gtest_discover_tests(unit_tests)
