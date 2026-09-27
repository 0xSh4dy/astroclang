/* A C translation unit that uses the library through its public interface.
 *
 * Together with c_lib.c this gives the C side two translation units and a
 * header, so cross-translation-unit resolution is exercised for C too - the
 * declarations behind c_lib.h are reached from here, not from where they are
 * defined.
 */

#include "c_lib.h"

#include <stdio.h>

/* A file-local helper.  `static` gives it internal linkage, which is part of
 * its identity: two translation units may each have their own `clamp`. */
static int clamp(int value, int low, int high) {
  if (value < low) {
    return low;
  }
  if (value > high) {
    return high;
  }
  return value;
}

/* A struct type local to this file. */
struct accumulator {
  int total;
  clib_count seen;
};

static int accumulate(struct clib_point *point, void *context) {
  struct accumulator *acc = (struct accumulator *)context;

  acc->total += clamp(point->x, 0, 100) + clamp(point->y, 0, 100);
  acc->seen++;

  return CLIB_OK;
}

int main(void) {
  struct clib_buffer buffer;
  struct accumulator acc;
  clib_count n;
  int status;

  clib_init(&buffer);

  status = clib_push(&buffer, CLIB_SQUARE(3), CLIB_SQUARE(4));
  if (status != CLIB_OK) {
    return status;
  }

  n = clib_size_of(&buffer);
  printf("pushed %lu points, total %lu\n", (unsigned long)n,
         (unsigned long)clib_total);

  /* The address of a function defined in the other translation unit.  The
   * call below is made by the library, not from here. */
  clib_set_visitor(&buffer, clib_sum_visitor, &acc.total);

  acc.total = 0;
  acc.seen = 0;
  clib_foreach(&buffer, accumulate, &acc);

  if (buffer.visit != 0) {
    buffer.visit(&buffer.items[0], buffer.visit_context);
  }

  return acc.total > 0 ? 0 : 1;
}
