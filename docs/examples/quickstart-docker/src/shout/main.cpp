// shout: print its arguments in capitals.
#include <cctype>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
  std::string out;  // qs: shout-main
  for (int i = 1; i < argc; ++i) {
    if (i > 1) out += ' ';
    for (unsigned char c : std::string(argv[i])) out += static_cast<char>(std::toupper(c));
  }
  std::cout << out << '\n';
  return 0;
}
