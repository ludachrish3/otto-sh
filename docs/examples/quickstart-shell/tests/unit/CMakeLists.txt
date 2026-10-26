# Unit tests for the text library. They link the same library wordcount does,
# so a run writes its counters into the build tree, which the unit tier reads.
add_executable(unit_tests test_text.cpp)
target_link_libraries(unit_tests PRIVATE textlib GTest::gtest_main)

# Register each TEST with CTest by listing the binary's tests after the build:
# a binary that holds no tests registers none, and `ctest --no-tests=error` fails.
include(GoogleTest)
gtest_discover_tests(unit_tests)
