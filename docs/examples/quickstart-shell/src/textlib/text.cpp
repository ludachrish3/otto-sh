#include "text.h"

#include <cctype>

namespace text {

int count_words(const std::string& line) {
  int words = 0;  // qs: count-words
  bool in_word = false;
  for (unsigned char c : line) {
    if (std::isspace(c)) {
      in_word = false;
    } else if (!in_word) {
      in_word = true;
      ++words;
    }
  }
  return words;
}

}  // namespace text
