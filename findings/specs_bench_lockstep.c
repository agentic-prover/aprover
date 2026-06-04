#include <assert.h>
int unknown1();
void main() {
  int i = 0, j = 0;
  while (unknown1()) {
    i = i + 1;
    j = j + 1;
  }
  static_assert(j >= 0);
}
