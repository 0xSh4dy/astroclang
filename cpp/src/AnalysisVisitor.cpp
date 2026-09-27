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

  // Skipping a subtree must not materialize a node for its root.  Every header
  // a translation unit includes contributes thousands of top-level
  // declarations, and none of them is part of the project.  A declaration that
  // is genuinely referenced becomes a node through reference() at the point of
  // use, which is both cheaper and more accurate: it records that something in
  // the project depends on it.
  if (!I.shouldTraverseInto(D)) return true;

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

void AnalysisVisitor::recordCall(SourceLocation Loc, const Decl *Callee) {
  if (Current.empty() || !Callee) return;

  std::string Flags;
  if (const auto *MD = dyn_cast<CXXMethodDecl>(Callee)) {
    // The call resolves to a specific declaration, but when that declaration
    // is virtual the runtime target may be an override.  Flagging it lets
    // impact analysis widen the result and label it as possible rather than
    // certain.
    if (MD->isVirtual()) {
      Flags = "\"virt\":1";
      if (MD->isPure()) Flags += ",\"pure\":1";
    }
  }

  const std::string &Target = I.reference(Callee);
  if (Target.empty()) return;
  // The edge points at the exact overload that overload resolution selected.
  // Linking that target back to its template pattern happens once per symbol
  // in Indexer::emitNode, not once per call site.
  I.addEdge("calls", Current, Target, Loc, Flags);
}

bool AnalysisVisitor::TraverseCallExpr(CallExpr *E) {
  if (!E) return true;
  return traverseCall(E, [this](CallExpr *X) {
    return RecursiveASTVisitor::TraverseCallExpr(X);
  });
}

bool AnalysisVisitor::TraverseCXXMemberCallExpr(CXXMemberCallExpr *E) {
  if (!E) return true;
  return traverseCall(E, [this](CXXMemberCallExpr *X) {
    return RecursiveASTVisitor::TraverseCXXMemberCallExpr(X);
  });
}

bool AnalysisVisitor::TraverseCXXOperatorCallExpr(CXXOperatorCallExpr *E) {
  if (!E) return true;
  return traverseCall(E, [this](CXXOperatorCallExpr *X) {
    return RecursiveASTVisitor::TraverseCXXOperatorCallExpr(X);
  });
}

bool AnalysisVisitor::TraverseUserDefinedLiteral(UserDefinedLiteral *E) {
  if (!E) return true;
  return traverseCall(E, [this](UserDefinedLiteral *X) {
    return RecursiveASTVisitor::TraverseUserDefinedLiteral(X);
  });
}

bool AnalysisVisitor::TraverseCUDAKernelCallExpr(CUDAKernelCallExpr *E) {
  if (!E) return true;
  return traverseCall(E, [this](CUDAKernelCallExpr *X) {
    return RecursiveASTVisitor::TraverseCUDAKernelCallExpr(X);
  });
}

bool AnalysisVisitor::VisitCallExpr(CallExpr *E) {
  if (const FunctionDecl *Direct = E->getDirectCallee()) {
    recordCall(E->getBeginLoc(), Direct);
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
  if (CXXConstructorDecl *Ctor = E->getConstructor())
    recordCall(E->getBeginLoc(), Ctor);
  return true;
}

// ---------------------------------------------------------------------------
// References
// ---------------------------------------------------------------------------

void AnalysisVisitor::recordReference(const ValueDecl *D, SourceLocation Loc) {
  if (Current.empty() || !D) return;

  // A callable reached as anything other than the callee of this call has had
  // its address taken - `apply(add, 3, 4)`, `auto f = &Foo::bar;`.  No `calls`
  // edge will ever be recorded for it, because which function runs is decided
  // at run time.  Dropping it would leave the function with no incoming edge at
  // all, and impact analysis silently blind to it; recording a reference marks
  // it as reachable without overstating that it is called.
  if (isa<FunctionDecl>(D) || isa<FunctionTemplateDecl>(D)) {
    const std::string &Target = I.reference(D);
    if (!Target.empty()) I.addEdge("references", Current, Target, Loc);
    return;
  }

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
  // The callee of the enclosing call already produced a `calls` edge.
  if (static_cast<const Expr *>(E) == Callee) return true;
  recordReference(E->getDecl(), E->getBeginLoc());
  return true;
}

bool AnalysisVisitor::VisitMemberExpr(MemberExpr *E) {
  // Only the member itself is skipped for `obj.method()`; the traversal still
  // descends into the base expression, so `obj` is recorded as referenced.
  if (static_cast<const Expr *>(E) != Callee)
    recordReference(E->getMemberDecl(), E->getBeginLoc());
  return true;
}

}  // namespace cg
