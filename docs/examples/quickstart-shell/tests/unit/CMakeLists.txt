# Unit tests for the text library. They link the same library wordcount does,
# so a run writes its counters into the build tree, which the unit tier reads.
add_executable(unit_tests test_text.cpp)
target_link_libraries(unit_tests PRIVATE textlib GTest::gtest_main)
add_test(NAME unit_tests COMMAND unit_tests)
