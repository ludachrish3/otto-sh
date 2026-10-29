# A third tool, also main.cpp, linking the text library: the singleton image's tool.
add_executable(banner main.cpp)
target_link_libraries(banner PRIVATE textlib)
