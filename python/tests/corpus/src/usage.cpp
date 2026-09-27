// Call sites, chosen so that each one resolves differently from its neighbours.
//
// The point of this file is that several calls look alike in the source and
// are different in the AST: `scale(2)` and `scale(2.0)` name the same function,
// `shape->area()` and `circle->area()` reach different declarations through the
// same spelling, and a call written inside a lambda runs somewhere else.

#include "shapes.h"

#include <vector>

namespace app {

/// Resolves to the int overload.  `scale(2)` and `scale(2.0)` differ only by
/// argument type, and only the AST knows which was chosen.
int use_int_overload() { return geo::scale(2); }

/// Resolves to the one-argument double overload.
double use_double_overload() { return geo::scale(2.0); }

/// Resolves to the two-argument overload.
double use_two_argument_overload() { return geo::scale(2.0, 3.0); }

/// A call through a base pointer.  The declaration reached is Shape::area -
/// pure virtual, no body - and the runtime target is whichever override the
/// object turns out to have.
double measure(const geo::Shape *shape) { return shape->area(); }

/// A call through a concrete object.  Same spelling, different target: this
/// one resolves to Circle::area and is not virtual dispatch in practice, even
/// though the method is virtual.
double measure_circle(const geo::Circle &circle) { return circle.area(); }

/// Two hops up: Tagged::area overrides Circle::area overrides Shape::area.
double measure_tagged(const geo::Tagged &tagged) { return tagged.area(); }

/// A qualified call, which bypasses virtual dispatch entirely and names the
/// base implementation.
double measure_base_explicitly(const geo::Tagged &tagged) {
  return tagged.geo::Circle::area();
}

/// The lambda's body calls area().  That call must be attributed to the
/// closure, not to this function: the closure may be invoked long after this
/// function has returned, or from another thread.
geo::Shape *pick_larger(geo::Shape *a, geo::Shape *b) {
  auto larger = [](geo::Shape *x, geo::Shape *y) -> geo::Shape * {
    return x->area() >= y->area() ? x : y;
  };
  return larger(a, b);
}

/// A named function used as a callable, then called through the variable.
/// The call site names `picker`, not `pick_by_name`, so the edge is indirect.
geo::Shape *pick_by_name(geo::Shape *a, geo::Shape *b) {
  return a->name()[0] >= b->name()[0] ? a : b;
}

geo::Shape *use_function_pointer(geo::Shape *a, geo::Shape *b) {
  geo::Shape *(*picker)(geo::Shape *, geo::Shape *) = &pick_by_name;
  return picker(a, b);
}

/// `auto`, a reference, and a pointer, all resolving to the same type.
auto doubled(int value) -> decltype(value) { return value * 2; }

int through_reference(const std::vector<int> &values) {
  const int &first = values.front();
  return first;
}

/// Template instantiation: the call resolves to Box<int>::get, an implicit
/// instantiation that exists only because this line exists.
int unbox(const geo::Box<int> &box) { return box.get(); }

double unbox_double(const geo::Box<double> &box) { return box.get(); }

/// A template function instantiated here.
int twice_int(int value) { return geo::twice(value); }

/// The nested class reached from outside, through its enclosing name.
int entry_weight(const geo::Registry::Entry &entry) { return entry.weight(); }

/// A static member called without an object.
geo::Registry &registry() { return geo::Registry::instance(); }

/// An overloaded operator, which is a call like any other and must resolve to
/// Registry::operator+=.
geo::Registry &add_entry(geo::Registry &registry, int weight) {
  return registry += geo::Registry::Entry(weight);
}

}  // namespace app
