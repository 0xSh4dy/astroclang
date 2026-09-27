#include "cg/CompilationDatabaseFactory.h"

#include <vector>

#include "clang/Tooling/CompilationDatabase.h"
#include "clang/Tooling/JSONCompilationDatabase.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"

using namespace clang;
using namespace clang::tooling;

namespace cg {

namespace {

/// A compilation database for a single file whose real configuration could not
/// be found.  Returning a hand-built CompileCommand is more predictable than
/// FixedCompilationDatabase, which applies the same command line to every file
/// it is asked about.
class FallbackDatabase : public CompilationDatabase {
public:
  FallbackDatabase(std::string Directory, std::string File,
                   std::vector<std::string> CommandLine)
      : Directory(std::move(Directory)), File(std::move(File)),
        CommandLine(std::move(CommandLine)) {}

  std::vector<CompileCommand> getCompileCommands(StringRef File) const override {
    // CompileCommand::CommandLine is the command line as executed, so the
    // source file belongs at the end of it.
    std::vector<std::string> Argv = CommandLine;
    Argv.push_back(File.str());
    CompileCommand Cmd(Directory, File, std::move(Argv), "");
    Cmd.Heuristic = "no compilation database; flags were synthesised";
    return {std::move(Cmd)};
  }
  std::vector<std::string> getAllFiles() const override { return {File}; }
  std::vector<CompileCommand> getAllCompileCommands() const override {
    return getCompileCommands(File);
  }

private:
  std::string Directory;
  std::string File;
  std::vector<std::string> CommandLine;
};

/// Language and standard to assume when nothing better is known.  Headers are
/// assumed to be C++: a project that ships a compile_commands.json is
/// overwhelmingly likely to be one, and `.h` is the extension C++ projects
/// use for their own headers.
void chooseLanguage(llvm::StringRef Path, std::string &Lang, std::string &Std) {
  llvm::StringRef Ext = llvm::sys::path::extension(Path).lower();
  if (Ext == ".c") {
    Lang = "c";
    Std = "-std=c11";
  } else if (Ext == ".h") {
    Lang = "c++-header";
    Std = "-std=c++17";
  } else if (Ext == ".hpp" || Ext == ".hh" || Ext == ".hxx" || Ext == ".h++" ||
             Ext == ".inl" || Ext == ".ipp" || Ext == ".tcc") {
    Lang = "c++-header";
    Std = "-std=c++17";
  } else if (Ext == ".m") {
    Lang = "objective-c";
    Std = "-std=c11";
  } else if (Ext == ".mm") {
    Lang = "objective-c++";
    Std = "-std=c++17";
  } else {
    Lang = "c++";
    Std = "-std=c++17";
  }
}

/// Directories that conventionally hold a project's own headers.  Only added
/// when they exist, so the fallback does not manufacture search paths that
/// would silently pick up unrelated files.
void addConventionalIncludeDirs(llvm::StringRef ProjectRoot,
                                std::vector<std::string> &Args) {
  if (ProjectRoot.empty()) return;
  static const char *Candidates[] = {"include", "inc", "src", "lib",
                                     "include/..", ""};
  for (const char *C : Candidates) {
    llvm::SmallString<256> P(ProjectRoot);
    if (*C) {
      llvm::sys::path::append(P, C);
    }
    if (llvm::sys::fs::is_directory(P)) {
      Args.push_back("-I" + std::string(P));
    }
  }
}

}  // namespace

const char *toString(ConfigSource S) {
  switch (S) {
    case ConfigSource::CompilationDatabase: return "compile_commands.json";
    case ConfigSource::AutoDetected: return "auto-detected";
    case ConfigSource::Fallback: return "fallback";
  }
  return "unknown";
}

CompilationConfig loadCompilationConfig(const std::string &SourceFile,
                                        const std::string &CompilationDatabaseDir,
                                        const std::string &CompilationDatabasePath,
                                        const std::string &ProjectRoot,
                                        const std::string &StandardOverride) {
  CompilationConfig Out;
  std::string Error;

  auto Accept = [&](std::unique_ptr<CompilationDatabase> DB, ConfigSource Src,
                    const std::string &Detail) -> bool {
    if (!DB) return false;
    // A database that does not mention this file is no more useful than no
    // database at all, and claiming otherwise would overstate accuracy.
    if (DB->getCompileCommands(SourceFile).empty()) return false;
    Out.DB = std::move(DB);
    Out.Source = Src;
    Out.Detail = Detail;
    return true;
  };

  if (!CompilationDatabasePath.empty()) {
    auto DB = JSONCompilationDatabase::loadFromFile(
        CompilationDatabasePath, Error, JSONCommandLineSyntax::AutoDetect);
    if (DB && Accept(std::move(DB), ConfigSource::CompilationDatabase,
                     CompilationDatabasePath))
      return Out;
    Out.Detail = "compile_commands.json at " + CompilationDatabasePath +
                 " does not cover this file";
  }

  if (!CompilationDatabaseDir.empty()) {
    auto DB = JSONCompilationDatabase::loadFromDirectory(CompilationDatabaseDir,
                                                         Error);
    if (DB && Accept(std::move(DB), ConfigSource::CompilationDatabase,
                     CompilationDatabaseDir + "/compile_commands.json"))
      return Out;
    Out.Detail = "no compile_commands.json under " + CompilationDatabaseDir;
  }

  {
    std::string AutoError;
    auto DB = CompilationDatabase::autoDetectFromSource(SourceFile, AutoError);
    if (DB && Accept(std::move(DB), ConfigSource::AutoDetected,
                     "auto-detected from " + SourceFile))
      return Out;
    if (Out.Detail.empty())
      Out.Detail = AutoError.empty() ? "no compilation database found" : AutoError;
  }

  // Fallback: synthesise something workable and make sure the caller can tell
  // that it happened.
  std::string Lang, Std;
  chooseLanguage(SourceFile, Lang, Std);
  if (!StandardOverride.empty()) Std = StandardOverride;
  Out.FallbackStandard = Std;

  std::vector<std::string> Args;
  Args.push_back("clang");
  Args.push_back("-x");
  Args.push_back(Lang);
  if (!Std.empty()) Args.push_back(Std);
  Args.push_back("-fsyntax-only");
  // Common predefined macros that a bare invocation would otherwise leave
  // undefined, which changes which branches of a header are even parsed.
  Args.push_back("-D__STDC_CONSTANT_MACROS");
  Args.push_back("-D__STDC_FORMAT_MACROS");
  Args.push_back("-D__STDC_LIMIT_MACROS");

  llvm::SmallString<256> Dir(SourceFile);
  llvm::sys::path::remove_filename(Dir);
  Args.push_back("-I" + std::string(Dir));
  addConventionalIncludeDirs(ProjectRoot, Args);

  Out.DB = std::make_unique<FallbackDatabase>(std::string(Dir), SourceFile, Args);
  Out.Source = ConfigSource::Fallback;
  return Out;
}

}  // namespace cg
