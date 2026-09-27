#pragma once

#include <string>

namespace cg {

/// Knobs controlling how much of a translation unit becomes graph nodes.
///
/// The defaults deliberately trade completeness for signal: a C++ TU pulls in
/// tens of thousands of standard-library declarations that are identical in
/// every project, and indexing them buries the project's own structure.  The
/// extractor still *resolves* references to those declarations, it just
/// records them as lightweight stubs rather than traversing them.
struct Options {
  /// Emit a node for every declaration found in a system header, instead of a
  /// stub only when something refers to it.
  bool IndexSystemHeaders = false;

  /// Record macro definitions (#define) as symbols.
  bool IndexMacros = true;

  /// Record function-local variables.
  bool IndexLocals = false;

  /// Record function parameters as symbols.
  bool IndexParameters = false;

  /// Walk into implicit template instantiations.  Expensive on template-heavy
  /// code; the instantiation actually used at a call site is recorded either
  /// way, because call resolution happens in the non-template caller.
  bool TemplateInstantiations = false;

  /// Emit nodes for compiler-generated declarations (implicit copy
  /// constructors, inherited constructors, ...).
  bool ImplicitDecls = false;

  /// How far to follow nested types (pointer -> element -> template argument)
  /// when recording type relationships.
  unsigned MaxTypeDepth = 4;

  /// Cap on the number of arguments recorded per function, to bound the cost
  /// of pathological declarations.
  unsigned MaxRecordedParams = 64;

  /// Project root, used to classify a file as belonging to the project or not
  /// when no compilation database entry marks it.
  std::string ProjectRoot;

  /// Echo captured diagnostics to stderr as well as into the fact stream.
  bool Verbose = false;
};

}  // namespace cg
