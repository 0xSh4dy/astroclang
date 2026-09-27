/* C definitions for c_lib.h.
 *
 * The interesting part is clib_foreach: it calls through its `fn` argument,
 * which is a parameter of function-pointer type.  No static analysis can say
 * which function that is, and the honest answer is an indirect call rather
 * than a guess or a silent omission.
 */

#include "c_lib.h"

clib_count clib_total = 0;

void clib_init(struct clib_buffer *buffer) {
  buffer->used = 0;
  buffer->visit = 0;
  buffer->visit_context = 0;
  clib_total = 0;
}

int clib_push(struct clib_buffer *buffer, int x, int y) {
  struct clib_point *slot;

  if (buffer->used >= CLIB_MAX_ITEMS) {
    return CLIB_FULL;
  }

  slot = &buffer->items[buffer->used];
  slot->x = x;
  slot->y = y;
  buffer->used++;
  clib_total++;

  return CLIB_OK;
}

clib_count clib_size_of(const struct clib_buffer *buffer) { return buffer->used; }

/* The call through fn: indirect, and recorded as such. */
int clib_foreach(struct clib_buffer *buffer, clib_visit_fn fn, void *context) {
  clib_count i;
  int last = CLIB_OK;

  for (i = 0; i < buffer->used; i++) {
    last = fn(&buffer->items[i], context);
    if (last != CLIB_OK) {
      return last;
    }
  }

  return CLIB_OK;
}

void clib_set_visitor(struct clib_buffer *buffer, clib_visit_fn fn, void *ctx) {
  buffer->visit = fn;
  buffer->visit_context = ctx;
}

/* Its address is taken in c_main.c, so it may be called from anywhere. */
int clib_sum_visitor(struct clib_point *point, void *context) {
  int *total = (int *)context;
  *total += point->x + point->y;
  return CLIB_OK;
}
