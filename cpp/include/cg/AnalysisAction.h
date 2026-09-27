// Frontend action wiring the extractor into Clang's parser.
//
// Two facts about this action are worth stating because they drive its shape:
//
//   * The preprocessor runs to completion *before* the AST is handed over, so
//     include and macro facts are emitted from PPCallbacks during parsing and
//     never wait on an ASTContext.
//   * A translation unit that fails to compile still produces an AST - a
//     partial one - and that partial graph is far more useful than nothing,
//     provided the failure is recorded alongside it.  Diagnostics are
//     therefore captured into the fact stream rather than merely printed.
#pragma once

#include <memory>
#include <string>

#include "cg/CompilationDatabaseFactory.h"
#include "cg/FactWriter.h"
#include "cg/Indexer.h"
#include "cg/Options.h"
#include "clang/Frontend/FrontendActions.h"

namespace clang {
class ASTConsumer;
class ASTContext;
class CompilerInstance;
}  // namespace clang

namespace cg {

class AnalysisAction : public clang::ASTFrontendAction {
public:
  /// `DroppedPCH` is the precompiled header the compilation database named and
  /// the extractor did not load, empty when it named none.  It is reported in
  /// the fact stream: the arguments this ran under are not the arguments the
  /// build used, and a consumer has to be able to see that.
  AnalysisAction(FactWriter &Writer, const Options &Opts, ConfigSource Source,
                 std::string SourceDetail, std::string DroppedPCH);

  bool BeginSourceFileAction(clang::CompilerInstance &CI) override;
  void EndSourceFileAction() override;
  std::unique_ptr<clang::ASTConsumer> CreateASTConsumer(
      clang::CompilerInstance &CI, llvm::StringRef InFile) override;

  /// Runs the semantic walk and writes nodes and edges for this TU.
  void runAnalysis(clang::ASTContext &Ctx);

  /// Called by the diagnostic consumer; errors mean the graph for this TU is
  /// incomplete and the index says so.
  void noteDiagnostic(bool IsError) {
    if (IsError) ++ErrorCount;
  }

private:
  FactWriter &W;
  const Options &Opts;
  ConfigSource Source;
  std::string SourceDetail;
  std::string DroppedPCH;
  std::unique_ptr<Indexer> Idx;
  unsigned ErrorCount = 0;
  std::string MainFile;
};

}  // namespace cg
