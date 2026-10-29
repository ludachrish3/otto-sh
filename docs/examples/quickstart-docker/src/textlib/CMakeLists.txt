# The text subsystem: a library in its own object directory, linked into wordcount.
add_library(textlib STATIC text.cpp)
target_include_directories(textlib PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
