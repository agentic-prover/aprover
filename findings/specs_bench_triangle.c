#include <stdio.h>

int validts(int a, int b, int c) {
    int valid = 0;
    if ((a+b>c) && (a+c>b) && (b+c>a) && 1) {
        valid = 1;
    } else {
        valid = 0;
    }

    return valid;
}

void test() {
    int valid = validts(2,3,4);
    //@ assert valid == 1;
}
