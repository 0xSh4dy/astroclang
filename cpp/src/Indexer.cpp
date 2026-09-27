#include "cg/Indexer.h"

#include <algorithm>

#include "clang/AST/ASTContext.h"
#include "clang/AST/Decl.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/DeclFriend.h"
#include "clang/AST/DeclOpenMP.h"
#include "clang/AST/DeclTemplate.h"
#include "clang/AST/ExprCXX.h"
#include "clang/AST/PrettyPrinter.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Index/USRGeneration.h"
#include "llvm/ADT/SmallString.h"

using namespace clang;

namespace cg {

namespace {

const std::string kEmpty;

/// A template and the declaration it templates describe a single entity: the
/// USR is generated for the templated declaration, so every other fact about
/// the node has to come from it too.  Mixing the two produces a node whose
/// identity names a function while its flags were read off a template
/// declaration, which is never a definition - so the body would go unrecorded.
const Decl *templatedDeclOf(const Decl *D) {
  if (const auto *CTD = dyn_cast<ClassTemplateDecl>(D))
    return CTD->getTemplatedDecl();
  if (const auto *FTD = dyn_cast<FunctionTemplateDecl>(D))
    return FTD->getTemplatedDecl();
  if (const auto *VTD = dyn_cast<VarTemplateDecl>(D))
    return VTD->getTemplatedDecl();
  if (const auto *ATD = dyn_cast<TypeAliasTemplateDecl>(D))
    return ATD->getTemplatedDecl();
  return D;
}

/// The definition of `D` when one is reachable from this redeclaration, else
/// nullptr.  Clang exposes this per declaration kind rather than on Decl, so
/// the kinds that can have a body are enumerated here.
const Decl *definitionOf(const Decl *D) {
  D = templatedDeclOf(D);
  if (auto *TD = dyn_cast<TagDecl>(D)) return TD->getDefinition();
  if (auto *FD = dyn_cast<FunctionDecl>(D)) return FD->getDefinition();
  if (auto *VD = dyn_cast<VarDecl>(D)) return VD->getDefinition();
  if (auto *ED = dyn_cast<EnumDecl>(D)) return ED->getDefinition();
  return nullptr;
}

bool isDefinition(const Decl *D) {
  if (const Decl *Def = definitionOf(D)) return Def == D;
  return false;
}

/// Templates and their specializations.  Kept separate from isDefinition
/// because an explicit specialization is a definition *and* a specialization.
bool isInstantiation(const Decl *D) {
  if (auto *CTSD = dyn_cast<ClassTemplateSpecializationDecl>(D))
    return CTSD->getSpecializationKind() == TSK_ImplicitInstantiation ||
           CTSD->getSpecializationKind() == TSK_ExplicitInstantiationDefinition ||
           CTSD->getSpecializationKind() == TSK_ExplicitInstantiationDeclaration;
  if (auto *FD = dyn_cast<FunctionDecl>(D)) {
    TemplateSpecializationKind K = FD->getTemplateSpecializationKind();
    return K == TSK_ImplicitInstantiation ||
           K == TSK_ExplicitInstantiationDefinition ||
           K == TSK_ExplicitInstantiationDeclaration;
  }
  return false;
}

bool isExplicitSpecialization(const Decl *D) {
  if (auto *CTSD = dyn_cast<ClassTemplateSpecializationDecl>(D))
    return CTSD->getSpecializationKind() == TSK_ExplicitSpecialization;
  if (auto *FD = dyn_cast<FunctionDecl>(D))
    return FD->getTemplateSpecializationKind() == TSK_ExplicitSpecialization;
  return false;
}

/// The template this declaration was instantiated from, if any.
const Decl *instantiationPattern(const Decl *D) {
  if (auto *CTSD = dyn_cast<ClassTemplateSpecializationDecl>(D))
    return CTSD->getSpecializedTemplate();
  if (auto *FD = dyn_cast<FunctionDecl>(D))
    return FD->getTemplateInstantiationPattern();
  if (auto *VD = dyn_cast<VarDecl>(D))
    return VD->getTemplateInstantiationPattern();
  return nullptr;
}

/// Declaration kinds that exist in every project and would otherwise flood the
/// graph with nodes nothing can usefully be asked about.
bool isNoise(const Decl *D) {
  return isa<StaticAssertDecl>(D) || isa<FileScopeAsmDecl>(D) ||
         isa<AccessSpecDecl>(D) || isa<FriendDecl>(D) ||
         isa<UsingDirectiveDecl>(D) || isa<UsingDecl>(D) ||
         isa<UsingShadowDecl>(D) || isa<IndirectFieldDecl>(D) ||
         isa<BindingDecl>(D) || isa<UnresolvedUsingValueDecl>(D) ||
         isa<UnresolvedUsingTypenameDecl>(D) || isa<ImportDecl>(D) ||
         isa<EmptyDecl>(D) || isa<OMPDeclareReductionDecl>(D) ||
         isa<OMPDeclareMapperDecl>(D) || isa<OMPThreadPrivateDecl>(D) ||
         isa<OMPAllocateDecl>(D) || isa<OMPCapturedExprDecl>(D) ||
         isa<OMPRequiresDecl>(D) || isa<MSPropertyDecl>(D) ||
         isa<LifetimeExtendedTemporaryDecl>(D);
}

/// Contexts that exist only to hold declarations and add no structure of their
/// own: `extern "C" { ... }` should not become a node.
bool isTransparent(const Decl *D) {
  return isa<LinkageSpecDecl>(D) || isa<ExportDecl>(D);
}

/// True when `D` declares something a caller could name.
bool introducesSymbol(const Decl *D) {
  if (isa<NamespaceDecl>(D) || isa<NamespaceAliasDecl>(D)) return true;
  if (isa<RecordDecl>(D) || isa<ClassTemplateDecl>(D)) return true;
  if (isa<EnumDecl>(D) || isa<EnumConstantDecl>(D)) return true;
  if (isa<FunctionDecl>(D) || isa<FunctionTemplateDecl>(D)) return true;
  if (isa<FieldDecl>(D)) return true;
  if (isa<TypedefNameDecl>(D)) return true;
  if (isa<VarDecl>(D)) return true;
  if (isa<TemplateTypeParmDecl>(D) || isa<NonTypeTemplateParmDecl>(D) ||
      isa<TemplateTemplateParmDecl>(D))
    return true;
  return false;
}

/// True when `DC` is, or is nested inside, an anonymous namespace.
///
/// Anonymity is inherited: a function in `namespace { namespace inner { ... } }`
/// has internal linkage just as much as one declared directly in the anonymous
/// namespace, and the check has to walk out to find it.
bool inAnonymousNamespace(const DeclContext *DC) {
  for (; DC; DC = DC->getParent()) {
    const auto *ND = dyn_cast<NamespaceDecl>(DC);
    if (ND && ND->isAnonymousNamespace()) return true;
  }
  return false;
}

}  // namespace

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

Indexer::Indexer(FactWriter &Writer, ASTContext &C, const Options &O)
    : W(Writer), Ctx(C), Opts(O) {}

SourceManager &Indexer::sm() const { return Ctx.getSourceManager(); }

size_t Indexer::EdgeKeyHash::operator()(const EdgeKey &K) const {
  size_t H = std::hash<std::string>()(K.Kind);
  auto Mix = [&H](size_t V) { H ^= V + 0x9e3779b97f4a7c15ULL + (H << 6) + (H >> 2); };
  Mix(std::hash<std::string>()(K.Src));
  Mix(std::hash<std::string>()(K.Dst));
  Mix(std::hash<int>()(K.File));
  return H;
}

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

std::string Indexer::computeUSR(const Decl *D) {
  // Normalising here means a visitor that reaches both a template and its
  // templated declaration produces one node rather than two.
  const Decl *Target = templatedDeclOf(D);

  llvm::SmallString<160> Buf;
  if (index::generateUSRForDecl(Target, Buf)) return std::string();
  return std::string(Buf.str());
}

std::string Indexer::computeSyntheticUSR(const Decl *D) {
  // Reached only for declarations Clang declines to give a USR, which in
  // practice means anonymous entities.  The identity is TU-local by
  // construction, so it is marked as such rather than pretending to be
  // mergeable across translation units.
  std::string Out = "c:syn";
  if (const auto *ND = dyn_cast<NamedDecl>(D)) {
    std::string Q = ND->getQualifiedNameAsString();
    if (!Q.empty()) {
      Out += ':';
      Out += Q;
    }
  }
  Loc L = locOf(D->getLocation());
  Out += "#f";
  Out += std::to_string(L.File);
  Out += "#l";
  Out += std::to_string(L.Line);
  Out += "#c";
  Out += std::to_string(L.Col);
  return Out;
}

const std::string *Indexer::usrOf(const Decl *D) {
  if (!D) return nullptr;
  // Redeclarations share a USR; collapsing to the canonical declaration first
  // makes the cache effective instead of storing an entry per redeclaration.
  const Decl *Canon = D->getCanonicalDecl();
  auto It = USRCache.find(Canon);
  if (It != USRCache.end()) return It->second.empty() ? nullptr : &It->second;

  std::string U = computeUSR(Canon);
  if (U.empty() && Canon != D) U = computeUSR(D);
  USRCache.emplace(Canon, U);
  return U.empty() ? nullptr : &USRCache.find(Canon)->second;
}

const std::string &Indexer::usrOrSynthetic(const Decl *D) {
  if (const std::string *U = usrOf(D)) return *U;
  const Decl *Canon = D->getCanonicalDecl();
  // usrOf() caches the *failure* as an empty string, so a hit here does not
  // mean a usable identity was found.
  auto It = USRCache.find(Canon);
  if (It == USRCache.end()) It = USRCache.emplace(Canon, std::string()).first;
  if (It->second.empty()) {
    It->second = computeSyntheticUSR(D);
    ++St.SyntheticIDs;
  }
  return It->second;
}

// ---------------------------------------------------------------------------
// Locations
// ---------------------------------------------------------------------------

Indexer::Loc Indexer::locOf(SourceLocation L) const {
  Loc Out;
  if (L.isInvalid()) return Out;
  SourceLocation Spelling = sm().getSpellingLoc(L);
  if (Spelling.isInvalid()) return Out;
  Out.File = const_cast<FactWriter &>(W).internLocation(sm(), L);
  if (Out.File < 0) return Loc{};  // not backed by a real file
  PresumedLoc PL = sm().getPresumedLoc(Spelling);
  if (PL.isInvalid()) return Loc{};
  Out.Line = static_cast<int>(PL.getLine());
  Out.Col = static_cast<int>(PL.getColumn());
  return Out;
}

// ---------------------------------------------------------------------------
// Classification
// ---------------------------------------------------------------------------

llvm::StringRef Indexer::kindOf(const Decl *D) {
  if (isa<NamespaceDecl>(D)) return "namespace";
  if (isa<NamespaceAliasDecl>(D)) return "namespace_alias";
  if (isa<ClassTemplateDecl>(D)) {
    return cast<ClassTemplateDecl>(D)->getTemplatedDecl()->getKindName();
  }
  if (const auto *RD = dyn_cast<RecordDecl>(D)) return RD->getKindName();
  if (isa<EnumDecl>(D)) return "enum";
  if (isa<EnumConstantDecl>(D)) return "enumerator";
  if (isa<CXXConstructorDecl>(D)) return "constructor";
  if (isa<CXXDestructorDecl>(D)) return "destructor";
  if (isa<CXXConversionDecl>(D)) return "conversion_function";
  if (isa<CXXMethodDecl>(D)) return "method";
  if (isa<FunctionTemplateDecl>(D)) return "function";
  if (isa<FunctionDecl>(D)) return "function";
  if (isa<FieldDecl>(D)) return "field";
  if (isa<TypedefNameDecl>(D)) return "typedef";
  if (isa<VarDecl>(D)) return "variable";
  if (isa<TemplateTypeParmDecl>(D) || isa<NonTypeTemplateParmDecl>(D) ||
      isa<TemplateTemplateParmDecl>(D))
    return "template_parameter";
  return "unknown";
}

bool Indexer::isInSystemHeader(const Decl *D) const {
  SourceLocation L = D->getLocation();
  if (L.isInvalid()) return false;
  return sm().isInSystemHeader(sm().getSpellingLoc(L));
}

std::string Indexer::accessOf(const Decl *D) const {
  const auto *ND = dyn_cast<NamedDecl>(D);
  if (!ND) return {};
  // Only class members have an access level.  Clang reports one for template
  // parameters and parameters too, inherited from the surrounding declaration,
  // and recording it would make `access` mean "some enclosing scope" instead of
  // what a reader expects: how this member is reached.
  if (isa<ParmVarDecl>(D) || isa<TemplateTypeParmDecl>(D) ||
      isa<NonTypeTemplateParmDecl>(D) || isa<TemplateTemplateParmDecl>(D))
    return {};
  switch (ND->getAccess()) {
    case AS_public: return "pub";
    case AS_protected: return "prot";
    case AS_private: return "priv";
    case AS_none: return {};
  }
  return {};
}

/// True when `D` is a declaration this indexer will materialize as a node.
bool Indexer::isIndexableDecl(const Decl *D) const {
  if (!D || isNoise(D)) return false;
  if (isa<TranslationUnitDecl>(D) || isTransparent(D)) return false;
  if (!introducesSymbol(D)) return false;
  if (D->isImplicit() && !Opts.ImplicitDecls) {
    // Implicit instantiations are not marked isImplicit(), so this only
    // filters compiler-synthesised declarations such as implicit copy
    // constructors.
    return false;
  }

  // Locals and parameters are opt-in: a large TU contains orders of magnitude
  // more of them than everything else combined.
  if (const auto *VD = dyn_cast<VarDecl>(D)) {
    if (VD->isLocalVarDecl() && !Opts.IndexParameters) {
      if (!Opts.IndexLocals) return false;
    }
    if (isa<ParmVarDecl>(D) && !Opts.IndexParameters) return false;
  }

  if (!Opts.IndexSystemHeaders && isInSystemHeader(D)) return false;
  return true;
}

bool Indexer::shouldTraverseInto(const Decl *D) const {
  if (!D) return false;
  if (isa<TranslationUnitDecl>(D) || isTransparent(D)) return true;
  // A system header's inline bodies describe the standard library, not the
  // project.  Descending into `std::vector<T>` to walk every member of every
  // instantiation buries a project's own code under the standard library and
  // costs most of the index time.  References *to* those declarations are
  // still resolved by reference(), which is what a query actually needs: the
  // caller wants to know that `v.resize(3)` reaches `std::vector<int>::resize`,
  // not to read that method's body.
  if (!Opts.IndexSystemHeaders && isInSystemHeader(D)) return false;
  return true;
}

// ---------------------------------------------------------------------------
// Node construction
// ---------------------------------------------------------------------------

std::string Indexer::buildFlags(const Decl *D) const {
  std::string F;
  auto Add = [&F](llvm::StringRef Key, bool Value) {
    if (!Value) return;
    if (!F.empty()) F += ',';
    F += '"';
    F += Key;
    F += "\":1";
  };

  Add("def", isDefinition(D));
  Add("tmpl", D->isTemplated());
  Add("inst", isInstantiation(D));
  Add("spec", isExplicitSpecialization(D));
  Add("impl", D->isImplicit());
  Add("sys", isInSystemHeader(D));
  Add("deprecated", D->isDeprecated());

  if (const auto *RD = dyn_cast<RecordDecl>(D)) {
    Add("anon", RD->isAnonymousStructOrUnion());
    if (const auto *CXX = dyn_cast<CXXRecordDecl>(D)) {
      // Whether a class is abstract or polymorphic is a property of its
      // definition.  A forward declaration has no answer, and Clang asserts
      // rather than inventing one, so the flag is left off.  "Not stated" is
      // the honest result; "not abstract" would be a claim the AST cannot back.
      if (CXX->hasDefinition()) {
        Add("abstract", CXX->isAbstract());
        Add("polymorphic", CXX->isPolymorphic());
      }
      Add("lambda", CXX->isLambda());
      Add("union", CXX->isUnion());
    }
  }

  if (const auto *FD = dyn_cast<FunctionDecl>(D)) {
    Add("deleted", FD->isDeleted());
    Add("defaulted", FD->isDefaulted());
    Add("variadic", FD->isVariadic());
    Add("inline", FD->isInlineSpecified());
    Add("constexpr", FD->isConstexpr());
    Add("extern_c", FD->isExternC());
    // A free function declared `static` - and anything in an anonymous
    // namespace - has internal linkage.  Two translation units may each have
    // their own `clamp` and they are not the same function; a reader deciding
    // whether a change is local needs to know which of the two they are
    // looking at.
    Add("static", FD->getStorageClass() == SC_Static ||
                      inAnonymousNamespace(FD->getDeclContext()));
    if (const auto *MD = dyn_cast<CXXMethodDecl>(D)) {
      Add("virtual", MD->isVirtual());
      Add("pure", MD->isPure());
      Add("static", MD->isStatic());
      Add("const", MD->isConst());
      // A method that overrides something is exactly the set impact analysis
      // needs to expand virtual dispatch through.
      Add("override", MD->size_overridden_methods() > 0);
      const auto *PD = dyn_cast<CXXConstructorDecl>(MD);
      Add("explicit", PD && PD->isExplicit());
    }
  }

  if (const auto *VD = dyn_cast<VarDecl>(D)) {
    Add("static", VD->isStaticLocal() || VD->getStorageClass() == SC_Static);
    Add("constexpr", VD->isConstexpr());
    Add("extern", VD->hasExternalStorage());
  }

  if (const auto *TD = dyn_cast<TypedefNameDecl>(D)) Add("using", isa<TypeAliasDecl>(TD));

  if (const auto *ED = dyn_cast<EnumDecl>(D)) {
    Add("scoped", ED->isScoped());
    Add("fixed", ED->isFixed());
  }

  std::string Acc = accessOf(D);
  if (!Acc.empty()) {
    if (!F.empty()) F += ',';
    F += "\"access\":\"";
    F += Acc;
    F += '"';
  }
  return F;
}

std::string Indexer::signatureOf(const Decl *D) const {
  const auto *FD = dyn_cast<FunctionDecl>(D);
  if (!FD) {
    if (const auto *FTD = dyn_cast<FunctionTemplateDecl>(D))
      FD = dyn_cast<FunctionDecl>(FTD->getTemplatedDecl());
  }
  if (!FD) return std::string();

  PrintingPolicy PP = Ctx.getPrintingPolicy();
  PP.SuppressTagKeyword = true;
  PP.SuppressScope = false;
  PP.Bool = true;
  PP.SuppressDefaultTemplateArgs = false;
  PP.SuppressUnwrittenScope = true;

  std::string Out = "(";
  bool First = true;
  unsigned Count = 0;
  for (const ParmVarDecl *P : FD->parameters()) {
    if (Count++ >= Opts.MaxRecordedParams) {
      Out += ", ...";
      break;
    }
    if (!First) Out += ", ";
    First = false;
    Out += P->getType().getAsString(PP);
  }
  // A single unnamed `void` parameter reads better as an empty list, matching
  // how the declaration is normally written in C.
  if (FD->getNumParams() == 1 && Out == "(void)") Out = "()";
  Out += ")";

  if (const auto *MD = dyn_cast<CXXMethodDecl>(FD)) {
    if (MD->isConst()) Out += " const";
    if (MD->isVolatile()) Out += " volatile";
    if (MD->isStatic()) Out += " static";
    switch (MD->getRefQualifier()) {
      case RQ_LValue: Out += " &"; break;
      case RQ_RValue: Out += " &&"; break;
      default: break;
    }
  }
  return Out;
}

std::string Indexer::qualifiedNameOf(const NamedDecl *ND) const {
  // getQualifiedNameAsString() prints a specialization's name through
  // printName(), which does not append its template arguments.  Members of a
  // specialization get them anyway, because the arguments come from the
  // nested-name-specifier the enclosing class prints - so `Box<int>::get` is
  // right while `Box<int>` itself comes out as `Box`, and the primary
  // template, `Box<int>` and `Box<double>` all become the same name.
  //
  // getNameForDiagnostic() is the printer that appends them, and it is the
  // only one that does.
  PrintingPolicy PP = Ctx.getPrintingPolicy();
  PP.SuppressTagKeyword = true;
  std::string S;
  llvm::raw_string_ostream OS(S);
  ND->getNameForDiagnostic(OS, PP, /*Qualified=*/true);
  OS.flush();
  return S;
}

/// True when a type as written tells a reader nothing about what it denotes.
///
/// `auto doubled(int) -> decltype(value)` has a return type of `int`, and the
/// spelling is the one thing about it that is not informative: the question a
/// reader brings to a return type is what values may come back, and
/// `decltype(value)` answers it only if they can see the declaration of
/// `value`.  `auto` alone is worse still.
bool isUninformativeSpelling(llvm::StringRef Text) {
  return Text == "auto" || Text.starts_with("decltype(") ||
         Text.starts_with("__decltype(");
}

std::string Indexer::typeTextOf(const Decl *D) const {
  PrintingPolicy PP = Ctx.getPrintingPolicy();
  PP.SuppressTagKeyword = true;
  PP.Bool = true;
  PP.SuppressUnwrittenScope = true;

  QualType T;
  if (const auto *FD = dyn_cast<FunctionDecl>(D)) {
    T = FD->getReturnType();
  } else if (const auto *FTD = dyn_cast<FunctionTemplateDecl>(D)) {
    if (const auto *FD = dyn_cast<FunctionDecl>(FTD->getTemplatedDecl()))
      T = FD->getReturnType();
  } else if (const auto *TD = dyn_cast<TypedefNameDecl>(D)) {
    T = TD->getUnderlyingType();
  } else if (const auto *VD = dyn_cast<ValueDecl>(D)) {
    T = VD->getType();
  }

  if (T.isNull()) return std::string();

  std::string Written = T.getAsString(PP);
  if (!isUninformativeSpelling(Written)) return Written;

  // The canonical type is fully desugared, so it is exact but verbose:
  // `std::__cxx11::basic_string<char>` rather than `std::string`.  That is the
  // right trade only here, where the alternative is a type that says nothing.
  return T.getCanonicalType().getAsString(PP);
}

std::string Indexer::parentUSROf(const Decl *D) {
  const DeclContext *DC = D->getDeclContext();
  while (DC && DC->isTransparentContext()) DC = DC->getParent();
  if (!DC) return std::string();
  const auto *PD = dyn_cast<Decl>(DC);
  if (!PD || isa<TranslationUnitDecl>(PD)) return std::string();
  return reference(PD);
}

FactWriter::Symbol Indexer::buildSymbol(const Decl *D, const std::string &USR,
                                        bool Stub) const {
  FactWriter::Symbol S;
  S.USR = USR;
  S.Stub = Stub;
  S.Kind = kindOf(D);
  if (const auto *ND = dyn_cast<NamedDecl>(D)) {
    S.Name = ND->getNameAsString();
    S.QualifiedName = qualifiedNameOf(ND);
  }
  S.Signature = signatureOf(D);
  S.TypeText = typeTextOf(D);

  Loc DeclLoc = locOf(D->getLocation());
  S.File = DeclLoc.File;
  S.Line = DeclLoc.Line;
  S.Col = DeclLoc.Col;

  // The definition may live in a different file than the declaration, and the
  // node is more useful pointing at the definition.
  if (const Decl *Def = definitionOf(D)) {
    Loc DL = locOf(Def->getLocation());
    if (DL.File >= 0) {
      S.DefFile = DL.File;
      S.DefLine = DL.Line;
      S.DefCol = DL.Col;
      SourceRange R = Def->getSourceRange();
      PresumedLoc End = sm().getPresumedLoc(sm().getSpellingLoc(R.getEnd()));
      if (End.isValid()) {
        S.EndLine = static_cast<int>(End.getLine());
        S.EndCol = static_cast<int>(End.getColumn());
      }
    }
  }

  S.ParentUSR = const_cast<Indexer *>(this)->parentUSROf(D);
  S.Flags = buildFlags(D);
  return S;
}

// ---------------------------------------------------------------------------
// Node emission
// ---------------------------------------------------------------------------

const std::string &Indexer::emitNode(const Decl *Original, bool Full) {
  const Decl *D = templatedDeclOf(Original);
  const std::string &U = usrOrSynthetic(D);
  if (U.empty()) return kEmpty;

  bool AlreadyFull = FullNodes.count(U) != 0;
  if (AlreadyFull) return U;
  bool AlreadyStub = StubNodes.count(U) != 0;
  if (AlreadyStub && !Full) return U;

  W.emitSymbol(buildSymbol(D, U, /*Stub=*/!Full));
  ++St.Symbols;
  if (Full) {
    FullNodes.insert(U);
  } else {
    StubNodes.insert(U);
  }
  if (!CanonicalDecl.count(U)) CanonicalDecl.emplace(U, D->getCanonicalDecl());

  // An implicit instantiation is a distinct entity from its template, but a
  // query about the template should also reach the instantiations. Recording
  // the link once per symbol (rather than once per call site) keeps the edge
  // count proportional to the code, not to how often it runs.
  if (PatternLinked.insert(U).second) {
    if (const Decl *Pat = instantiationPattern(D)) {
      const std::string &PatUSR = reference(Pat);
      if (!PatUSR.empty() && PatUSR != U) addEdge("specializes", U, PatUSR, D->getLocation());
    }
  }
  return U;
}

const std::string &Indexer::declare(const Decl *D) {
  if (!isIndexableDecl(D)) return kEmpty;
  const std::string &U = emitNode(D, true);
  if (U.empty() || !ExtrasDone.insert(U).second) return U;

  // Templates and their templated declarations share an identity but not a
  // class; work from whichever one actually carries the structure.
  const Decl *SD = templatedDeclOf(D);

  const std::string Parent = parentUSROf(D);
  if (!Parent.empty()) addEdge("contains", Parent, U, D->getLocation());

  // -- declared types ------------------------------------------------------
  if (const auto *FD = dyn_cast<FunctionDecl>(SD)) {
    addTypeEdges(U, FD->getReturnType(), "returns", FD->getLocation());
    unsigned Count = 0;
    for (const ParmVarDecl *P : FD->parameters()) {
      if (Count++ >= Opts.MaxRecordedParams) break;
      addTypeEdges(U, P->getType(), "param_type", P->getLocation());
    }
  } else if (const auto *TD = dyn_cast<TypedefNameDecl>(SD)) {
    addTypeEdges(U, TD->getUnderlyingType(), "aliases", TD->getLocation());
  } else if (const auto *VD = dyn_cast<ValueDecl>(SD)) {
    // Fields and globals both tell a reader what type a name holds; the kinds
    // differ so a query can ask for one without the other.
    const llvm::StringRef Kind = isa<FieldDecl>(SD) ? "field_type" : "var_type";
    addTypeEdges(U, VD->getType(), Kind, SD->getLocation());
  }

  // -- inheritance ---------------------------------------------------------
  // Base clauses are held in the definition data.  A class that was only
  // forward-declared in this translation unit has none to read - and a
  // declaration written *with* a base clause does have them, which is why the
  // test is hasDefinition() and not isCompleteDefinition().
  const auto *CXX = dyn_cast<CXXRecordDecl>(SD);
  if (CXX && CXX->hasDefinition()) {
    for (const CXXBaseSpecifier &B : CXX->bases()) {
      // Dependent bases (`template<class T> struct D : T`) have no record to
      // point at until instantiation; there is nothing honest to record.
      const CXXRecordDecl *Base = B.getType()->getAsCXXRecordDecl();
      if (!Base) continue;
      const std::string &BaseUSR = reference(Base);
      if (BaseUSR.empty() || BaseUSR == U) continue;

      std::string Flags = "\"acc\":\"";
      switch (B.getAccessSpecifier()) {
        case AS_public: Flags += "pub"; break;
        case AS_protected: Flags += "prot"; break;
        case AS_private: Flags += "priv"; break;
        case AS_none: Flags += "none"; break;
      }
      Flags += '"';
      if (B.isVirtual()) Flags += ",\"virtual\":1";
      if (B.isPackExpansion()) Flags += ",\"pack\":1";
      addEdge("inherits", U, BaseUSR, B.getBaseTypeLoc(), Flags);

      // `class D : public Base<int>` derives from the instantiation, but a
      // reader asking "what derives from Base" means the template.
      if (const Decl *Pat = instantiationPattern(Base))
        addEdge("instantiates", U, reference(Pat), B.getBaseTypeLoc());
    }
  }

  // -- virtual overrides ---------------------------------------------------
  if (const auto *MD = dyn_cast<CXXMethodDecl>(SD)) {
    for (const CXXMethodDecl *OM : MD->overridden_methods()) {
      const std::string &OvrUSR = reference(OM);
      if (!OvrUSR.empty() && OvrUSR != U)
        addEdge("overrides", U, OvrUSR, MD->getLocation());
    }
  }

  return U;
}

const std::string &Indexer::reference(const Decl *D) {
  if (!D) return kEmpty;
  if (isNoise(D) || isa<TranslationUnitDecl>(D)) return kEmpty;
  if (D->isImplicit() && !Opts.ImplicitDecls) {
    // Referring to an implicit declaration should still resolve, but to the
    // declaration it was derived from rather than to a node of its own.
    if (const Decl *Pattern = instantiationPattern(D)) return reference(Pattern);
    return kEmpty;
  }
  return emitNode(D, false);
}

const std::string &Indexer::referenceSynthesized(const Decl *D) {
  if (!D || isNoise(D) || isa<TranslationUnitDecl>(D)) return kEmpty;
  return emitNode(D, false);
}

// ---------------------------------------------------------------------------
// Edges
// ---------------------------------------------------------------------------

void Indexer::addEdge(llvm::StringRef Kind, const std::string &Src,
                      const std::string &Dst, SourceLocation L,
                      llvm::StringRef Flags, int Weight) {
  if (Src.empty() || Dst.empty() || Src == Dst) return;
  Loc Where = locOf(L);

  EdgeKey Key{Kind.str(), Src, Dst, Where.File};
  auto It = EdgeIndex.find(Key);
  if (It != EdgeIndex.end()) {
    // Same caller, same target, same file: fold into one weighted edge. A
    // function that calls another in a loop should not produce one record per
    // iteration.
    FactWriter::Edge &E = Edges[It->second];
    E.Count += Weight;
    if (!Flags.empty() && E.Flags.empty()) E.Flags = Flags.str();
    return;
  }

  EdgeIndex.emplace(std::move(Key), Edges.size());
  FactWriter::Edge E;
  E.Kind = Kind;
  E.Src = Src;
  E.Dst = Dst;
  E.File = Where.File;
  E.Line = Where.Line;
  E.Count = Weight;
  if (!Flags.empty()) E.Flags = Flags.str();
  Edges.push_back(std::move(E));
}

void Indexer::flushEdges() {
  // Insertion order is deterministic for a given input, which keeps the fact
  // stream stable enough to assert on in tests.
  for (const FactWriter::Edge &E : Edges) W.emitEdge(E);
  St.Edges += Edges.size();
  Edges.clear();
  EdgeIndex.clear();
}

// ---------------------------------------------------------------------------
// Type relationships
// ---------------------------------------------------------------------------

void Indexer::addTypeEdges(const std::string &SrcUSR, QualType T,
                           llvm::StringRef Kind, SourceLocation Loc) {
  if (SrcUSR.empty() || T.isNull()) return;
  std::unordered_set<const void *> Seen;
  addTypeEdgesImpl(SrcUSR, T, Kind, Loc, 0, Seen);
}

void Indexer::addTypeEdgesImpl(const std::string &SrcUSR, QualType T,
                               llvm::StringRef Kind, SourceLocation Loc,
                               unsigned Depth, std::unordered_set<const void *> &Seen) {
  if (T.isNull() || Depth > Opts.MaxTypeDepth) return;

  // Strip the sugar that carries no referent of its own. Elaborated types
  // (`struct Foo`), attributed types and parentheses all describe the same
  // entity as what they wrap.
  QualType Canonical = T.getCanonicalType();
  if (Canonical.isNull()) return;
  if (!Seen.insert(Canonical.getTypePtr()).second) return;  // cycle guard

  // Pointer/reference/array/cv all still refer to the element type; a graph
  // user asking "what type does this field hold" wants the pointee.
  if (const auto *PT = T->getAs<PointerType>()) {
    return addTypeEdgesImpl(SrcUSR, PT->getPointeeType(), Kind, Loc, Depth + 1, Seen);
  }
  if (const auto *RT = T->getAs<ReferenceType>()) {
    return addTypeEdgesImpl(SrcUSR, RT->getPointeeType(), Kind, Loc, Depth + 1, Seen);
  }
  if (const auto *AT = T->getAsArrayTypeUnsafe()) {
    return addTypeEdgesImpl(SrcUSR, AT->getElementType(), Kind, Loc, Depth + 1, Seen);
  }
  if (const auto *MPT = T->getAs<MemberPointerType>()) {
    return addTypeEdgesImpl(SrcUSR, MPT->getPointeeType(), Kind, Loc, Depth + 1, Seen);
  }
  // `auto x = f();` is only useful once the deduced type is substituted in.
  if (const auto *AT = T->getAs<AutoType>()) {
    if (!AT->getDeducedType().isNull())
      return addTypeEdgesImpl(SrcUSR, AT->getDeducedType(), Kind, Loc, Depth + 1, Seen);
  }
  if (const auto *ET = T->getAs<ElaboratedType>()) {
    return addTypeEdgesImpl(SrcUSR, ET->getNamedType(), Kind, Loc, Depth + 1, Seen);
  }
  if (const auto *ST = T->getAs<SubstTemplateTypeParmType>()) {
    if (!ST->getReplacementType().isNull())
      return addTypeEdgesImpl(SrcUSR, ST->getReplacementType(), Kind, Loc, Depth + 1, Seen);
  }
  if (T->isFunctionProtoType()) {
    const auto *FPT = T->castAs<FunctionProtoType>();
    addTypeEdgesImpl(SrcUSR, FPT->getReturnType(), Kind, Loc, Depth + 1, Seen);
    for (QualType PT : FPT->getParamTypes())
      addTypeEdgesImpl(SrcUSR, PT, Kind, Loc, Depth + 1, Seen);
    return;
  }

  // A named declaration: this is the edge we actually wanted.
  if (const auto *RT = T->getAs<RecordType>()) {
    if (const Decl *D = RT->getDecl()) {
      addEdge(Kind, SrcUSR, reference(D), Loc);
      // Going through the underlying template as well is what lets an impact
      // query for `std::vector` reach users that only ever wrote
      // `std::vector<int>`.
      if (const Decl *Pat = instantiationPattern(D))
        addEdge("instantiates", SrcUSR, reference(Pat), Loc);
    }
    return;
  }
  if (const auto *ET = T->getAs<EnumType>()) {
    if (const Decl *D = ET->getDecl()) addEdge(Kind, SrcUSR, reference(D), Loc);
    return;
  }
  // A typedef is a node in its own right; the storage layer links it onward to
  // its underlying type via the `aliases` edge, so the chain is preserved
  // without flattening it here.
  if (const auto *TT = T->getAs<TypedefType>()) {
    if (const Decl *D = TT->getDecl()) addEdge(Kind, SrcUSR, reference(D), Loc);
    return;
  }
  if (const auto *TST = T->getAs<TemplateSpecializationType>()) {
    if (const TemplateDecl *TD = TST->getTemplateName().getAsTemplateDecl()) {
      addEdge(Kind, SrcUSR, reference(TD), Loc);
      unsigned ArgCount = 0;
      for (const TemplateArgument &A : TST->template_arguments()) {
        if (ArgCount++ >= Opts.MaxRecordedParams) break;
        if (A.getKind() == TemplateArgument::Type)
          addTypeEdgesImpl(SrcUSR, A.getAsType(), Kind, Loc, Depth + 1, Seen);
        else if (A.getKind() == TemplateArgument::Template &&
                 A.getAsTemplate().getAsTemplateDecl())
          addEdge(Kind, SrcUSR, reference(A.getAsTemplate().getAsTemplateDecl()), Loc);
      }
    }
    return;
  }
  if (const auto *IT = T->getAs<InjectedClassNameType>()) {
    if (const Decl *D = IT->getDecl()) addEdge(Kind, SrcUSR, reference(D), Loc);
    return;
  }
  // Everything else (builtins, dependent types, ...) has no declaration to
  // point at.
}

// ---------------------------------------------------------------------------
// Diagnostics and metadata
// ---------------------------------------------------------------------------

}  // namespace cg
