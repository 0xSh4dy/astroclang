// Compiler-argument discovery.
//
// Semantic C++ analysis is only as correct as the flags it runs under.  A
// translation unit compiled as C++17 with three `-I` paths and two `-D`s will
// resolve overloads, templates and includes completely differently from the
// same file compiled with none of them, and the failure mode is silent: the
// AST still exists, it is just wrong.
//
// So the compilation database is the primary input, and when one cannot be
// found the extractor says so explicitly rather than proceeding quietly.  The
// fact stream carries the provenance, and the index records it, which is what
// lets every later query report reduced accuracy instead of pretending.
#pragma once

#include <memory>
#include <string>

// Included rather than forward-declared: CompilationConfig owns the database
// through a unique_ptr, so its destructor needs the complete type.
#include "clang/Tooling/CompilationDatabase.h"

namespace cg {

/// Where a translation unit's compiler arguments came from.
enum class ConfigSource {
  /// Read from a compile_commands.json that actually lists this file.
  CompilationDatabase,
  /// Discovered by Clang's own search from the source file upward.
  AutoDetected,
  /// Synthesised: no database was found.  Include paths, defines and the
  /// language standard are guesses, so resolution is degraded.
  Fallback,
};

const char *toString(ConfigSource S);

struct CompilationConfig {
  std::unique_ptr<clang::tooling::CompilationDatabase> DB;
  ConfigSource Source = ConfigSource::Fallback;
  /// Path of the database used, or the reason the fallback was needed.
  std::string Detail;
  /// Language standard applied in fallback mode (empty otherwise).
  std::string FallbackStandard;

  /// True when resolution can be trusted for this translation unit.
  bool Accurate() const { return Source != ConfigSource::Fallback; }
};

/// Resolves the compiler arguments for `SourceFile`.
///
/// Lookup order: an explicit `--compdb` path, then `-p`/`--compdb-dir`, then
/// Clang's own auto-detection walking up from the source file, then a
/// synthesised fallback.
CompilationConfig loadCompilationConfig(const std::string &SourceFile,
                                        const std::string &CompilationDatabaseDir,
                                        const std::string &CompilationDatabasePath,
                                        const std::string &ProjectRoot,
                                        const std::string &StandardOverride);

}  // namespace cg
