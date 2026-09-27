#include "cg/AnalysisVisitor.h"

#include "clang/AST/Decl.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/DeclTemplate.h"

using namespace clang;

namespace cg {

// ---------------------------------------------------------------------------
// Declaration traversal
// ---------------------------------------------------------------------------

bool AnalysisVisitor::TraverseDecl(Decl *D) {
  if (!D) return true;

  // Declarations we are not indexing still need a node if something refers to
  // them, but walking their contents would describe the standard library
  // rather than the project.
  if (!I.shouldTraverseInto(D)) {
    I.reference(D);
    return true;
  }

  bool Pushed = false;
  std::string Saved;
  const std::string &U = I.declare(D);
  if (!U.empty()) {
    Saved.swap(Current);
    Current = U;
    Pushed = true;
  }

  bool Ok = RecursiveASTVisitor::TraverseDecl(D);

  if (Pushed) Current.swap(Saved);
  return Ok;
}

bool AnalysisVisitor::TraverseLambdaExpr(LambdaExpr *E) {
  if (!E) return true;
  // A lambda is a closure object with its own call operator.  Attributing its
  // body to the enclosing function would report the wrong caller for every
  // call made inside it.
  const std::string &U = I.reference(E->getLambdaClass());
  if (U.empty()) return RecursiveASTVisitor::TraverseLambdaExpr(E);

  std::string Saved;
  Saved.swap(Current);
  Current = U;
  bool Ok = RecursiveASTVisitor::TraverseLambdaExpr(E);
  Current.swap(Saved);
  return Ok;
}

// ---------------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------------

void AnalysisVisitor::recordCall(CallExpr *E, const Decl *Callee) {
  if (Current.empty() || !Callee) return;

  std::string Flags;
  if (const auto *MD = dyn_cast<CXXMethodDecl>(Callee)) {
    // The call resolves to a specific declaration, but when that declaration
    // is virtual the runtime target may be an override.  Flagging it lets
    // impact analysis widen the result and label it as possible rather than
    // certain.
    if (MD->isVirtual()) {
      Flags = "\"virt\":1";
      if (MD->isPureVirtual()) Flags += ",\"pure\":1";
    }
  }

  const std::string &Target = I.reference(Callee);
  if (Target.empty()) return;
  // The edge points at the exact overload that overload resolution selected.
  // Linking that target back to its template pattern happens once per symbol
  // in Indexer::emitNode, not once per call site.
  I.addEdge("calls", Current, Target, E->getBeginLoc(), Flags);
}

bool AnalysisVisitor::VisitCallExpr(CallExpr *E) {
  if (const FunctionDecl *Callee = E->getDirectCallee()) {
    recordCall(E, Callee);
    return true;
  }

  // No direct callee: the call goes through a function pointer, a member
  // pointer or `std::function`.  Recording the variable being called is a
  // weaker but still useful statement, and it is labelled as indirect so a
  // consumer never mistakes it for a resolved call.
  ++I.stats().UnresolvedCalls;
  Expr *CalleeExpr = E->getCallee();
  if (!CalleeExpr || Current.empty()) return true;
  CalleeExpr = CalleeExpr->IgnoreParenImpCasts();

  const ValueDecl *VD = nullptr;
  if (const auto *DRE = dyn_cast<DeclRefExpr>(CalleeExpr))
    VD = DRE->getDecl();
  else if (const auto *ME = dyn_cast<MemberExpr>(CalleeExpr))
    VD = ME->getMemberDecl();

  if (VD) {
    const std::string &Target = I.reference(VD);
    if (!Target.empty())
      I.addEdge("calls_indirect", Current, Target, E->getBeginLoc());
  }
  return true;
}

bool AnalysisVisitor::VisitCXXConstructExpr(CXXConstructExpr *E) {
  if (CXXConstructorDecl *Ctor = E->getConstructor()) recordCall(E, Ctor);
  return true;
}

// ---------------------------------------------------------------------------
// References
// ---------------------------------------------------------------------------

void AnalysisVisitor::recordReference(const ValueDecl *D, SourceLocation Loc) {
  if (Current.empty() || !D) return;
  // Calls already carry the relationship for callables.
  if (isa<FunctionDecl>(D)) return;

  // Function-local entities would drag every local variable into the graph;
  // they are only recorded when the caller asked for locals.
  const auto *VD = dyn_cast<VarDecl>(D);
  if (VD && VD->isLocalVarDecl() && !I.opts().IndexLocals) return;
  if (isa<ParmVarDecl>(D) && !I.opts().IndexParameters) return;
  if (isa<NonTypeTemplateParmDecl>(D) || isa<TemplateTypeParmDecl>(D) ||
      isa<TemplateTemplateParmDecl>(D))
    return;

  const std::string &Target = I.reference(D);
  if (Target.empty()) return;
  I.addEdge("references", Current, Target, Loc);
}

bool AnalysisVisitor::VisitDeclRefExpr(DeclRefExpr *E) {
  recordReference(E->getDecl(), E->getBeginLoc());
  return true;
}

bool AnalysisVisitor::VisitMemberExpr(MemberExpr *E) {
  recordReference(E->getMemberDecl(), E->getBeginLoc());
  return true;
}

}  // namespace cg
