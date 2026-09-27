// C++ constructs the index must resolve semantically.
//
// Every declaration here exists to make one relationship checkable from the
// outside: this override belongs to that base method, this call resolves to
// that overload, this name reaches that definition in another translation
// unit.  The file is deliberately small enough to hold in your head, because a
// failure in it should point at one construct rather than at a subsystem.

#pragma once

#include <cstddef>

namespace geo {

/// Abstract base: a pure virtual, a virtual with a body, and a non-virtual.
class Shape {
public:
  Shape();
  virtual ~Shape();

  /// Pure: every concrete shape must answer this.
  virtual double area() const = 0;

  /// Virtual with a definition, so a call may go to either implementation.
  virtual const char *name() const;

  int id() const { return id_; }

protected:
  int id_ = 0;
};

/// Single inheritance, overriding both virtuals.
class Circle : public Shape {
public:
  explicit Circle(double radius);
  ~Circle() override;

  double area() const override;
  const char *name() const override;

  double radius() const { return radius_; }

private:
  double radius_;
};

/// A second base, unrelated to Shape, so multiple inheritance is real.
class Named {
public:
  virtual ~Named();
  virtual const char *label() const;
};

/// Multiple inheritance, plus a diamond-free chain: Tagged::area overrides
/// Circle::area, which overrides Shape::area.  A change to the base must reach
/// both, and the two hops are different edges.
class Tagged : public Circle, public Named {
public:
  Tagged(double radius, int tag);
  ~Tagged() override;

  double area() const override;
  const char *label() const override;

private:
  int tag_;
};

// -- overloads --------------------------------------------------------------

/// Three functions sharing a name.  Which one a call resolves to is decided by
/// the argument types, and a graph keyed on the name alone cannot tell them
/// apart - the reason overload resolution has to come from the AST.
double scale(double value);
double scale(double value, double factor);
int scale(int value);

// -- templates --------------------------------------------------------------

template <typename T>
class Box {
public:
  void put(const T &value) { value_ = value; }
  T get() const { return value_; }

private:
  T value_{};
};

template <typename T>
T twice(T value) {
  return value + value;
}

// -- names, aliases, nesting, operators, statics ----------------------------

class Registry {
public:
  /// A nested class: its identity is qualified by the enclosing one.
  class Entry {
  public:
    explicit Entry(int weight);
    int weight() const;

  private:
    int weight_;
  };

  static Registry &instance();

  Registry &operator+=(const Entry &entry);

  /// An alias and an old-style typedef naming the same type.
  using Count = unsigned long;
  typedef int RawId;

  Count size() const;

private:
  Count count_ = 0;
};

}  // namespace geo
