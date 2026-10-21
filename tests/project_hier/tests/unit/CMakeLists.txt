# A gtest unit-test executable that compiles the library's source again, so
# a second core.cpp.gcno sits under the project root. Needs libgtest-dev
# (headers and libgtest_main) on the build machine.
find_package(GTest REQUIRED)
add_executable(unit_tests test_core.cpp ${PROJECT_SOURCE_DIR}/src/core/core.cpp)
target_include_directories(unit_tests PRIVATE ${PROJECT_SOURCE_DIR}/src/core)
target_link_libraries(unit_tests PRIVATE GTest::gtest_main)
