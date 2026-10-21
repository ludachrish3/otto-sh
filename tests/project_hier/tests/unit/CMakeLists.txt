# A plain unit-test executable that compiles the library's source again, so
# a second core.cpp.gcno sits under the project root.
add_executable(unit_tests test_core.cpp ${PROJECT_SOURCE_DIR}/src/core/core.cpp)
target_include_directories(unit_tests PRIVATE ${PROJECT_SOURCE_DIR}/src/core)
