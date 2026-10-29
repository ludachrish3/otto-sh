// The singleton image's tool: its arguments, after their word count.
#include <iostream>
#include <string>

#include "text.h"

int main(int argc, char** argv) {
  std::string line;
  for (int i = 1; i < argc; ++i) {
    if (i > 1) line += ' ';
    line += argv[i];
  }
  std::cout << "[" << text::count_words(line) << "] " << line << "\n";  // qs: banner-main
  return 0;
}
