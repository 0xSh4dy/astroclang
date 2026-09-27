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
#include <vector>

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

/// Compiler arguments with the precompiled-header ones taken out.
struct StrippedArguments {
  /// What to hand Clang.
  std::vector<std::string> Arguments;

  /// The precompiled header those arguments named, spelled as the database
  /// spelled it; empty when they named none.  The caller reports it.  A
  /// translation unit parsed without the precompiled header its build uses was
  /// parsed under arguments the build did not use, and that is the caller's to
  /// disclose rather than this function's to hide.
  std::string DroppedPCH;
};

/// Removes the arguments that make Clang load or build a precompiled header.
///
/// A precompiled header is an AST file, and Clang reads an AST file only when
/// it was written by the compiler reading it - the same version, built with the
/// same options.  A compilation database is the build's record of how the
/// project was compiled, and the compiler that wrote that record need not be
/// the one this extractor was built against.  When it is not, the header is
/// unreadable: Clang reports "malformed or corrupted AST file" as a diagnostic,
/// stops, and the translation unit yields nothing at all.
///
/// Nothing else in a compile command is version-locked this way.  The source,
/// the include paths, the defines and the language standard are all read by
/// every version, so removing these arguments is what lets one extractor serve
/// projects built by compilers other than its own.
///
/// When to do it is the caller's decision, and it is not one this function can
/// make for them: the header is unreadable for reasons only the reading
/// compiler can see.  A version comparison is not a substitute.  Clang's own
/// test is full version string equality against the value recorded inside the
/// AST file, and reading that value takes the same AST machinery that refuses
/// the file in the first place.  Attempting the parse and letting Clang object
/// is the authoritative check, and it also catches the causes a version
/// comparison would miss: a truncated header, or one whose recorded options no
/// longer match the command.
StrippedArguments stripPrecompiledHeaderArguments(std::vector<std::string> Args);

}  // namespace cg
