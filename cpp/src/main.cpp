// cg-index - semantic extractor for one C/C++ translation unit.
//
// Reads one source file, asks Clang to parse it under the arguments from the
// compilation database, and writes a fact stream (JSON Lines) to stdout or a
// file.  Everything else - storage, querying, MCP - lives in the Python layer.
//
// One translation unit per process is a deliberate choice.  It gives exact
// failure isolation (a TU that crashes loses only its own facts), makes the
// output of a single run small enough to assert on in tests, and lets the
// driver parallelise across TUs and re-index one TU without touching the rest
// of the index.

#include <cstdlib>
#include <memory>
#include <string>
#include <vector>

#include "cg/AnalysisAction.h"
#include "cg/CompilationDatabaseFactory.h"
#include "cg/Options.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"

using namespace clang;
using namespace clang::tooling;

namespace {

class AnalysisFactory : public FrontendActionFactory {
public:
  AnalysisFactory(cg::FactWriter &W, const cg::Options &Opts,
                  cg::ConfigSource Source, std::string Detail)
      : W(W), Opts(Opts), Source(Source), Detail(std::move(Detail)) {}

  std::unique_ptr<FrontendAction> create() override {
    return std::make_unique<cg::AnalysisAction>(W, Opts, Source, Detail);
  }

private:
  cg::FactWriter &W;
  const cg::Options &Opts;
  cg::ConfigSource Source;
  std::string Detail;
};

void printUsage(llvm::raw_ostream &OS) {
  OS << "cg-index - semantic fact extractor for one C/C++ translation unit\n"
        "\n"
        "usage: cg-index [options] <source-file>\n"
        "\n"
        "compilation configuration:\n"
        "  -p, --compdb-dir <dir>   directory containing compile_commands.json\n"
        "      --compdb <path>      explicit path to compile_commands.json\n"
        "      --project-root <dir> project root, used by the fallback config\n"
        "      --std <standard>     language standard for the fallback config\n"
        "      --print-config       report how arguments were found, then exit\n"
        "\n"
        "what to index:\n"
        "      --sys-headers        index symbols from system headers too\n"
        "      --locals             index function-local variables\n"
        "      --params             index function parameters\n"
        "      --no-macros          skip #define symbols\n"
        "      --template-instantiations  walk implicit template instantiations\n"
        "      --implicit-decls     include compiler-generated declarations\n"
        "      --max-type-depth <n> how far to follow nested types (default 4)\n"
        "\n"
        "output:\n"
        "  -o, --output <file>      write facts here instead of stdout\n"
        "      --stats              print extraction statistics to stderr\n"
        "  -v, --verbose            echo diagnostics to stderr\n"
        "  -h, --help               this message\n";
}

/// Minimal argument parsing.  LLVM's own option parser keeps global state that
/// would have to be reset between translation units in a long-lived process;
/// this tool is a one-shot binary, so hand-rolling is cheaper than fighting it.
struct Args {
  std::string SourceFile;
  std::string CompDBDir;
  std::string CompDBPath;
  std::string ProjectRoot;
  std::string Std;
  std::string Output;
  bool PrintConfig = false;
  bool ShowStats = false;
  bool Help = false;
};

bool parseArgs(int argc, char **argv, Args &A, cg::Options &Opts,
               std::string &Error) {
  auto Need = [&](int &I, const char *What) -> const char * {
    if (I + 1 >= argc) {
      Error = std::string("missing value for ") + What;
      return nullptr;
    }
    return argv[++I];
  };

  for (int I = 1; I < argc; ++I) {
    llvm::StringRef Arg = argv[I];
    if (Arg == "-h" || Arg == "--help") {
      A.Help = true;
    } else if (Arg == "-p" || Arg == "--compdb-dir") {
      const char *V = Need(I, "--compdb-dir");
      if (!V) return false;
      A.CompDBDir = V;
    } else if (Arg == "--compdb") {
      const char *V = Need(I, "--compdb");
      if (!V) return false;
      A.CompDBPath = V;
    } else if (Arg == "--project-root") {
      const char *V = Need(I, "--project-root");
      if (!V) return false;
      A.ProjectRoot = V;
    } else if (Arg == "--std") {
      const char *V = Need(I, "--std");
      if (!V) return false;
      A.Std = V;
    } else if (Arg == "-o" || Arg == "--output") {
      const char *V = Need(I, "--output");
      if (!V) return false;
      A.Output = V;
    } else if (Arg == "--max-type-depth") {
      const char *V = Need(I, "--max-type-depth");
      if (!V) return false;
      Opts.MaxTypeDepth = static_cast<unsigned>(std::strtoul(V, nullptr, 10));
    } else if (Arg == "--sys-headers") {
      Opts.IndexSystemHeaders = true;
    } else if (Arg == "--locals") {
      Opts.IndexLocals = true;
    } else if (Arg == "--params") {
      Opts.IndexParameters = true;
    } else if (Arg == "--no-macros") {
      Opts.IndexMacros = false;
    } else if (Arg == "--template-instantiations") {
      Opts.TemplateInstantiations = true;
    } else if (Arg == "--implicit-decls") {
      Opts.ImplicitDecls = true;
    } else if (Arg == "--print-config") {
      A.PrintConfig = true;
    } else if (Arg == "--stats") {
      A.ShowStats = true;
    } else if (Arg == "-v" || Arg == "--verbose") {
      Opts.Verbose = true;
    } else if (Arg.starts_with("-")) {
      Error = "unknown option: " + Arg.str();
      return false;
    } else if (A.SourceFile.empty()) {
      A.SourceFile = Arg.str();
    } else {
      Error = "more than one source file given; cg-index handles one at a time";
      return false;
    }
  }
  return true;
}

}  // namespace

int main(int argc, char **argv) {
  Args A;
  cg::Options Opts;
  std::string Error;

  if (!parseArgs(argc, argv, A, Opts, Error)) {
    llvm::errs() << "cg-index: " << Error << "\n";
    return 2;
  }
  if (A.Help) {
    printUsage(llvm::outs());
    return 0;
  }
  if (A.SourceFile.empty()) {
    printUsage(llvm::errs());
    return 2;
  }

  // Make the source path absolute: every location in the fact stream is
  // relative to the process working directory otherwise, and the driver may
  // run the extractor from anywhere.
  llvm::SmallString<256> Abs(A.SourceFile);
  if (std::error_code EC = llvm::sys::fs::make_absolute(Abs)) {
    llvm::errs() << "cg-index: cannot resolve " << A.SourceFile << ": "
                 << EC.message() << "\n";
    return 2;
  }
  A.SourceFile = std::string(Abs);

  if (A.ProjectRoot.empty()) {
    llvm::SmallString<256> Root(A.SourceFile);
    llvm::sys::path::remove_filename(Root);
    A.ProjectRoot = std::string(Root);
  }
  Opts.ProjectRoot = A.ProjectRoot;

  cg::CompilationConfig Config = cg::loadCompilationConfig(
      A.SourceFile, A.CompDBDir, A.CompDBPath, A.ProjectRoot, A.Std);
  if (!Config.DB) {
    llvm::errs() << "cg-index: no usable compiler arguments for " << A.SourceFile
                 << "\n";
    return 3;
  }

  if (A.PrintConfig) {
    llvm::outs() << "{\"file\":\"" << A.SourceFile
                 << "\",\"config_source\":\"" << cg::toString(Config.Source)
                 << "\",\"detail\":\"" << Config.Detail << "\"}\n";
    return 0;
  }

  std::unique_ptr<llvm::raw_fd_ostream> FileOut;
  if (!A.Output.empty()) {
    std::error_code EC;
    FileOut = std::make_unique<llvm::raw_fd_ostream>(A.Output, EC,
                                                     llvm::sys::fs::OF_Text);
    if (EC) {
      llvm::errs() << "cg-index: cannot write " << A.Output << ": "
                   << EC.message() << "\n";
      return 2;
    }
  }
  llvm::raw_ostream &OS = FileOut ? static_cast<llvm::raw_ostream &>(*FileOut)
                                  : llvm::outs();

  cg::FactWriter Writer(OS);
  AnalysisFactory Factory(Writer, Opts, Config.Source, Config.Detail);

  ClangTool Tool(*Config.DB, {A.SourceFile});
  Tool.setPrintErrorMessage(Opts.Verbose);
  // ClangTool returns 1 on any diagnostic error.  A partial AST is still worth
  // indexing, so the exit code is reported through the fact stream (the "done"
  // and "errors" records) and a parse failure is not treated as extractor
  // failure here.
  Tool.run(&Factory);

  OS.flush();
  if (A.ShowStats) {
    llvm::errs() << "cg-index: " << A.SourceFile << " ("
                 << cg::toString(Config.Source) << ")\n";
  }
  return 0;
}
