#include <assert.h>

int max2(int x, int y) {
  return x >= y ? x : y;
}

void check_max2(int x, int y) {
  int r = max2(x, y);
  assert(r >= x);
  assert(r >= y);
  assert(r == x || r == y);
}
