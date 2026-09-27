/* C constructs the index must handle.
 *
 * C is not a smaller C++: there are no namespaces or methods, identity comes
 * from the tag namespace, and the interesting edges run through function
 * pointers, which no syntactic parser can resolve.
 */

#ifndef C_LIB_H
#define C_LIB_H

#include <stddef.h>

/* An object-like macro and a function-like one.  Macros leave no trace in the
 * AST after preprocessing, so what is recorded is their definition; a call
 * through one is indistinguishable from the expanded code and is not claimed
 * to be a call. */
#define CLIB_MAX_ITEMS 64
#define CLIB_SQUARE(x) ((x) * (x))

/* A struct with a tag, and a typedef naming an anonymous one. */
struct clib_point {
  int x;
  int y;
};

typedef struct {
  int width;
  int height;
} clib_size;

typedef unsigned long clib_count;

/* An enum, both plain and scoped-by-convention. */
enum clib_status { CLIB_OK = 0, CLIB_FULL = 1, CLIB_EMPTY = 2 };

/* A callback type: the call through it cannot be resolved statically, which
 * is exactly the case that must be reported as indirect rather than dropped. */
typedef int (*clib_visit_fn)(struct clib_point *point, void *context);

struct clib_buffer {
  struct clib_point items[CLIB_MAX_ITEMS];
  clib_count used;
  clib_visit_fn visit;
  void *visit_context;
};

/* A global with external linkage, defined in c_lib.c. */
extern clib_count clib_total;

/* Functions. */
void clib_init(struct clib_buffer *buffer);
int clib_push(struct clib_buffer *buffer, int x, int y);
clib_count clib_size_of(const struct clib_buffer *buffer);

/* Calls its argument once per item.  The call inside is indirect. */
int clib_foreach(struct clib_buffer *buffer, clib_visit_fn fn, void *context);

/* Takes a function pointer and stores it. */
void clib_set_visitor(struct clib_buffer *buffer, clib_visit_fn fn, void *ctx);

/* A function whose address is taken elsewhere, so it may be called from a
 * place the index cannot see. */
int clib_sum_visitor(struct clib_point *point, void *context);

#endif /* C_LIB_H */
