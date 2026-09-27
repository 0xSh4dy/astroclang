// Out-of-line definitions for shapes.h.
//
// A method declared in the header and defined here is one symbol with two
// locations.  Reporting the header's line sends a reader to a declaration they
// cannot read anything from; the definition is where the body is.

#include "shapes.h"

namespace geo {

Shape::Shape() = default;
Shape::~Shape() = default;

const char *Shape::name() const { return "shape"; }

Circle::Circle(double radius) : radius_(radius) { id_ = 1; }
Circle::~Circle() = default;

double Circle::area() const { return 3.14159265358979 * radius_ * radius_; }

const char *Circle::name() const { return "circle"; }

Named::~Named() = default;

const char *Named::label() const { return "named"; }

Tagged::Tagged(double radius, int tag) : Circle(radius), tag_(tag) {}
Tagged::~Tagged() = default;

// Overrides Circle::area, which overrides Shape::area.
double Tagged::area() const { return Circle::area() * 2.0; }

const char *Tagged::label() const { return "tagged"; }

// Three overloads of one name.  Each gets its own identity.
double scale(double value) { return value * 2.0; }

double scale(double value, double factor) { return value * factor; }

int scale(int value) { return value * 2; }

Registry::Entry::Entry(int weight) : weight_(weight) {}

int Registry::Entry::weight() const { return weight_; }

Registry &Registry::instance() {
  static Registry singleton;
  return singleton;
}

Registry &Registry::operator+=(const Entry &entry) {
  count_ += static_cast<Count>(entry.weight());
  return *this;
}

Registry::Count Registry::size() const { return count_; }

}  // namespace geo
