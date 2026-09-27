#include "cg/AnalysisAction.h"

#include <vector>

#include "cg/AnalysisVisitor.h"
#include "clang/AST/ASTConsumer.h"
#include "clang/AST/ASTContext.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Lex/MacroInfo.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "llvm/ADT/SmallString.h"
#include "llvm/Support/Path.h"

using namespace clang;

namespace cg {

namespace {

/// Emits include and macro facts while the preprocessor runs.
///
/// The include target here is the FileEntry the preprocessor actually opened,
/// after search-path resolution - not the string that was written in the
/// source.  That distinction is the whole reason include-based impact analysis
/// works at all for C++, where a header is reached through `-I` paths and
/// relative traversal that no amount of text matching can reconstruct.
class PreprocessorCollector : public PPCallbacks {
public:
  PreprocessorCollector(FactWriter &W, SourceManager &SM, const Options &Opts)
      : W(W), SM(SM), Opts(Opts) {}

  void InclusionDirective(SourceLocation HashLoc, const Token &,
                          StringRef FileName, bool IsAngled,
                          CharSourceRange, OptionalFileEntryRef File,
                          StringRef SearchPath, StringRef RelativePath,
                          const Module *, SrcMgr::CharacteristicKind) override {
    int From = W.internLocation(SM, HashLoc);
    if (From < 0) return;
    if (!File) {
      // An unresolved include is a real fact: it is usually the first symptom
      // of a missing `-I`, and it explains every symbol that goes missing
      // downstream of it.
      ++UnresolvedIncludes;
      return;
    }
    int To = W.internFileEntry(*File, SM.isInSystemHeader(HashLoc));
    if (To < 0) return;

    PresumedLoc PL = SM.getPresumedLoc(SM.getSpellingLoc(HashLoc));
    int Line = PL.isValid() ? static_cast<int>(PL.getLine()) : 0;
    W.emitInclude(From, To, Line, IsAngled, FileName);
  }

  void MacroDefined(const Token &MacroNameTok,
                    const MacroDirective *MD) override {
    if (!Opts.IndexMacros || !MD) return;
    const MacroInfo *MI = MD->getMacroInfo();
    if (!MI || MI->isBuiltinMacro()) return;
    SourceLocation Loc = MI->getDefinitionLoc();
    if (Loc.isInvalid()) return;
    // Macros from system headers are the compiler's own vocabulary
    // (`__GNUC__`, `NULL`, ...) and drown out the project's.
    if (SM.isInSystemHeader(SM.getSpellingLoc(Loc)) && !Opts.IndexSystemHeaders)
      return;

    int File = W.internLocation(SM, Loc);
    if (File < 0) return;
    PresumedLoc PL = SM.getPresumedLoc(SM.getSpellingLoc(Loc));
    if (!PL.isValid()) return;

    // Macros have no USR.  The definition site is their identity, which merges
    // correctly across the translation units that share a header and keeps
    // per-TU redefinitions distinct.
    std::string USR = "m:";
    USR += W.path(File);
    USR += ':';
    USR += std::to_string(PL.getLine());
    USR += ':';
    USR += MacroNameTok.getIdentifierInfo()->getName().str();

    FactWriter::Symbol S;
    S.USR = std::move(USR);
    S.Kind = "macro";
    S.Name = MacroNameTok.getIdentifierInfo()->getName().str();
    S.QualifiedName = S.Name;
    S.File = File;
    S.Line = static_cast<int>(PL.getLine());
    S.Col = static_cast<int>(PL.getColumn());
    S.Flags = MI->isFunctionLike() ? "\"function_like\":1" : "\"object_like\":1";
    if (MI->isVariadic()) S.Flags += ",\"variadic\":1";
    W.emitSymbol(S);
  }

  unsigned unresolvedIncludes() const { return UnresolvedIncludes; }

private:
  FactWriter &W;
  SourceManager &SM;
  const Options &Opts;
  unsigned UnresolvedIncludes = 0;
};

/// Turns Clang's diagnostics into facts and counts the fatal ones.
class FactDiagnosticConsumer : public DiagnosticConsumer {
public:
  FactDiagnosticConsumer(FactWriter &W, SourceManager &SM, AnalysisAction &A,
                         bool EchoToStderr)
      : W(W), SM(SM), Action(A), Echo(EchoToStderr) {}

  void HandleDiagnostic(DiagnosticsEngine::Level Level,
                        const Diagnostic &Info) override {
    bool IsError = Level >= DiagnosticsEngine::Error;
    Action.noteDiagnostic(IsError);

    // Remarks and notes are noise; warnings and errors explain why the graph
    // for this translation unit may be incomplete.
    if (Level < DiagnosticsEngine::Warning) return;

    llvm::SmallString<256> Msg;
    Info.FormatDiagnostic(Msg);

    int File = W.internLocation(SM, Info.getLocation());
    PresumedLoc PL;
    if (Info.getLocation().isValid())
      PL = SM.getPresumedLoc(SM.getSpellingLoc(Info.getLocation()));

    W.emitDiag(severityName(Level), File, PL.isValid() ? PL.getLine() : 0,
               PL.isValid() ? PL.getColumn() : 0, std::string(Msg));

    if (Echo) {
      llvm::errs() << "astroclang-index: " << severityName(Level) << ": " << Msg << '\n';
    }
  }

private:
  static llvm::StringRef severityName(DiagnosticsEngine::Level Level) {
    switch (Level) {
      case DiagnosticsEngine::Ignored: return "ignored";
      case DiagnosticsEngine::Note: return "note";
      case DiagnosticsEngine::Remark: return "remark";
      case DiagnosticsEngine::Warning: return "warning";
      case DiagnosticsEngine::Error: return "error";
      case DiagnosticsEngine::Fatal: return "fatal";
    }
    return "unknown";
  }

  FactWriter &W;
  SourceManager &SM;
  AnalysisAction &Action;
  bool Echo;
};

class AnalysisConsumer : public ASTConsumer {
public:
  explicit AnalysisConsumer(AnalysisAction &A) : Action(A) {}
  void HandleTranslationUnit(ASTContext &Ctx) override {
    Action.runAnalysis(Ctx);
  }

private:
  AnalysisAction &Action;
};

}  // namespace

AnalysisAction::AnalysisAction(FactWriter &Writer, const Options &O,
                               ConfigSource Src, std::string Detail,
                               std::string Dropped)
    : W(Writer), Opts(O), Source(Src), SourceDetail(std::move(Detail)),
      DroppedPCH(std::move(Dropped)) {}

bool AnalysisAction::BeginSourceFileAction(CompilerInstance &CI) {
  SourceManager &SM = CI.getSourceManager();

  // The translation unit's path, spelled the way every location in this
  // stream will spell it.
  //
  // getCurrentFile() returns the path exactly as the compilation database
  // wrote it, and a database of relative entries writes paths relative to the
  // build directory.  A source location, meanwhile, is spelled by the file
  // interner, which resolves symlinks and `..`.  Those are different strings
  // for one file, and a consumer keying on the path would then hold the
  // translation unit under one identity and its own symbols under another -
  // leaving the file looking empty.  Interning the main file here reuses the
  // single function that decides how a path is spelled.
  const int MainID =
      W.internLocation(SM, SM.getLocForStartOfFile(SM.getMainFileID()));
  MainFile = MainID >= 0 ? W.path(MainID) : std::string(getCurrentFile());

  W.emitMeta("tu", MainFile);
  W.emitMeta("config_source", toString(Source));
  W.emitMeta("config_detail", SourceDetail);
  if (Source == ConfigSource::Fallback) {
    // The Python layer reads this to mark every symbol from this TU as
    // resolved under guessed configuration.
    W.emitMeta("degraded", "1");
  }
  if (!DroppedPCH.empty()) {
    // Reported, not merely done.  Dropping a precompiled header does not make
    // the parse worse - it is what makes it happen at all when the header was
    // written by another compiler - but it does mean this translation unit was
    // parsed under arguments the build did not use, and only the reader can
    // decide whether that matters for the question being asked.
    W.emitMeta("pch_dropped", DroppedPCH);
  }

  // Diagnostics are replaced rather than wrapped.  ClangTool created its
  // client without ownership, so taking over here leaves the tool's own client
  // alive and untouched for it to finish with.
  CI.getDiagnostics().setClient(
      new FactDiagnosticConsumer(W, SM, *this, Opts.Verbose),
      /*ShouldOwnClient=*/true);

  CI.getPreprocessor().addPPCallbacks(
      std::make_unique<PreprocessorCollector>(W, SM, Opts));
  return true;
}

std::unique_ptr<ASTConsumer> AnalysisAction::CreateASTConsumer(
    CompilerInstance &, llvm::StringRef) {
  return std::make_unique<AnalysisConsumer>(*this);
}

void AnalysisAction::runAnalysis(ASTContext &Ctx) {
  Idx = std::make_unique<Indexer>(W, Ctx, Opts);
  AnalysisVisitor Visitor(*Idx);
  Visitor.TraverseDecl(Ctx.getTranslationUnitDecl());
  Idx->flushEdges();
}

void AnalysisAction::EndSourceFileAction() {
  if (Idx) {
    const Indexer::Stats &S = Idx->stats();
    W.emitMetaRaw("stats",
                  "{\"symbols\":" + std::to_string(S.Symbols) +
                      ",\"edges\":" + std::to_string(S.Edges) +
                      ",\"includes\":" + std::to_string(W.includeCount()) +
                      ",\"unresolved_calls\":" +
                      std::to_string(S.UnresolvedCalls) +
                      ",\"synthetic_ids\":" + std::to_string(S.SyntheticIDs) +
                      "}");
  }
  W.emitMetaRaw("errors", "{\"errors\":" + std::to_string(ErrorCount) + "}");
  W.emitDone();
}

}  // namespace cg
