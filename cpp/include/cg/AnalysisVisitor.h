// AST traversal producing graph edges.
//
// The visitor keeps one piece of state: the symbol currently being walked
// ("Current").  Everything a statement does is attributed to it, so a call
// inside a method body becomes `method --calls--> callee`.  Entering a
// declaration that became a node pushes it; leaving pops it.  Declarations
// that did not become nodes (locals, noise) leave the stack untouched, which
// is why a call inside a nested block is still attributed to its function.
#pragma once

#include <string>

#include "cg/Indexer.h"
#include "clang/AST/Expr.h"
#include "clang/AST/ExprCXX.h"
#include "clang/AST/RecursiveASTVisitor.h"

namespace cg {

class AnalysisVisitor : public clang::RecursiveASTVisitor<AnalysisVisitor> {
public:
  explicit AnalysisVisitor(Indexer &I) : I(I) {}

  // Implicit instantiations are only walked when asked for: they multiply the
  // graph by the number of distinct template arguments in the project.
  bool shouldVisitTemplateInstantiations() const {
    return I.opts().TemplateInstantiations;
  }
  // Compiler-synthesised bodies (implicit copy constructors, ...) describe the
  // language, not the program.
  bool shouldVisitImplicitCode() const { return false; }

  bool TraverseDecl(clang::Decl *D);
  bool TraverseLambdaExpr(clang::LambdaExpr *E);

  // RecursiveASTVisitor generates a separate traversal for each call
  // expression subclass, so overriding CallExpr alone would only cover plain
  // calls - `obj.method()` and `a + b` would keep their duplicate edges.
  bool TraverseCallExpr(clang::CallExpr *E);
  bool TraverseCXXMemberCallExpr(clang::CXXMemberCallExpr *E);
  bool TraverseCXXOperatorCallExpr(clang::CXXOperatorCallExpr *E);
  bool TraverseUserDefinedLiteral(clang::UserDefinedLiteral *E);
  bool TraverseCUDAKernelCallExpr(clang::CUDAKernelCallExpr *E);

  bool VisitCallExpr(clang::CallExpr *E);
  bool VisitCXXConstructExpr(clang::CXXConstructExpr *E);
  bool VisitDeclRefExpr(clang::DeclRefExpr *E);
  bool VisitMemberExpr(clang::MemberExpr *E);

  /// Symbol the traversal is currently inside; empty at namespace scope.
  const std::string &current() const { return Current; }

private:
  /// Marks `E`'s callee as spoken for, runs the base traversal, then restores
  /// the previous mark.  `Base` is the RecursiveASTVisitor traversal matching
  /// the concrete expression type.
  template <typename CallT, typename Fn>
  bool traverseCall(CallT *E, Fn Base) {
    const clang::Expr *Saved = Callee;
    clang::Expr *CalleeExpr = E->getCallee();
    Callee = CalleeExpr ? CalleeExpr->IgnoreParenImpCasts() : nullptr;
    bool Ok = Base(E);
    Callee = Saved;
    return Ok;
  }

  /// Records a resolved call to `Callee`, including the virtual-dispatch and
  /// instantiation information an impact analysis needs.
  void recordCall(clang::SourceLocation Loc, const clang::Decl *Callee);

  /// Emits a `references` edge unless the target is a callable, in which case
  /// the call path owns the relationship.
  void recordReference(const clang::ValueDecl *D, clang::SourceLocation Loc);

  Indexer &I;
  std::string Current;

  /// The callee sub-expression of the call currently being traversed.
  ///
  /// A call's callee is reached twice: once as the target of `calls` from
  /// VisitCallExpr, and again as an ordinary expression when the traversal
  /// descends into it.  Without this, every direct call would also be recorded
  /// as a reference.  Tracking the expression rather than suppressing all
  /// function references is what lets an address-taken function - `apply(add,
  /// 3, 4)` - still be recorded, since there the name is *not* a callee and its
  /// address genuinely escapes.
  const clang::Expr *Callee = nullptr;
};

}  // namespace cg
