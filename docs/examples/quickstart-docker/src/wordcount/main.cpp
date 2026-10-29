// wordcount: print how many words its arguments hold.
#include <iostream>
#include <string>

#include "text.h"

int main(int argc, char** argv) {
  std::string line;  // qs: wordcount-main
  for (int i = 1; i < argc; ++i) {
    if (i > 1) line += ' ';
    line += argv[i];
  }
  std::cout << text::count_words(line) << '\n';
  return 0;
}
