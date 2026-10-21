# libcore.so: reached by `app` only through its DT_NEEDED entry.
add_library(core SHARED core.cpp)
target_include_directories(core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
