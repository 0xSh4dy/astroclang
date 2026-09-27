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

  bool VisitCallExpr(clang::CallExpr *E);
  bool VisitCXXConstructExpr(clang::CXXConstructExpr *E);
  bool VisitDeclRefExpr(clang::DeclRefExpr *E);
  bool VisitMemberExpr(clang::MemberExpr *E);

  /// Symbol the traversal is currently inside; empty at namespace scope.
  const std::string &current() const { return Current; }

private:
  /// Records a resolved call to `Callee`, including the virtual-dispatch and
  /// instantiation information an impact analysis needs.
  void recordCall(clang::CallExpr *E, const clang::Decl *Callee);

  /// Emits a `references` edge unless the target is a callable, in which case
  /// the call path owns the relationship.
  void recordReference(const clang::ValueDecl *D, clang::SourceLocation Loc);

  Indexer &I;
  std::string Current;
};

}  // namespace cg
