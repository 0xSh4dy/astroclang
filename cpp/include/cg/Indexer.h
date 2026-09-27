// Semantic model construction.
//
// The Indexer is the single place that decides what a Clang declaration looks
// like as a graph node, and how two declarations relate.  It owns:
//
//   * symbol identity  - a Clang USR, which already distinguishes namespaces,
//                        classes, overloads and template arguments, so the
//                        graph never has to guess from a spelling;
//   * node emission    - one record per declaration per translation unit,
//                        deduplicated by USR;
//   * edge emission    - aggregated per (kind, source, target, file) so that a
//                        function called two hundred times in a loop produces
//                        one weighted edge rather than two hundred records.
//
// Redundancy across translation units is intentional.  A header declaration is
// re-emitted by every TU that includes it; the storage layer merges those
// records into one node while keeping a per-TU occurrence list, which is what
// makes incremental re-indexing of a single TU correct.
#pragma once

#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "cg/FactWriter.h"
#include "cg/Options.h"
#include "clang/AST/Type.h"
#include "llvm/ADT/StringRef.h"

namespace clang {
class ASTContext;
class Decl;
class NamedDecl;
class QualType;
class SourceLocation;
class SourceManager;
}  // namespace clang

namespace cg {

class Indexer {
public:
  Indexer(FactWriter &Writer, clang::ASTContext &Ctx, const Options &Opts);

  // -- identity ------------------------------------------------------------

  /// Stable identity for `D`, or nullptr when it has none (a handful of
  /// implicit declarations).  The returned pointer is owned by the indexer.
  const std::string *usrOf(const clang::Decl *D);

  /// Same as usrOf but never null: falls back to a synthetic, TU-local
  /// identity so that anonymous entities still participate in the graph.
  const std::string &usrOrSynthetic(const clang::Decl *D);

  // -- nodes ---------------------------------------------------------------

  /// Emits the full node for `D` if it is worth indexing, and returns its USR.
  /// Returns an empty string when `D` is not indexable.
  const std::string &declare(const clang::Decl *D);

  /// Ensures `D` has a node, emitting a minimal stub when the declaration
  /// lives in a header we are not traversing.  This is what keeps
  /// `callers(std::vector<int>::resize)` answerable without indexing libstdc++.
  const std::string &reference(const clang::Decl *D);

  /// True when `D` should become a node of its own.
  bool isIndexableDecl(const clang::Decl *D) const;

  /// True when the visitor should descend into `D`'s children.
  bool shouldTraverseInto(const clang::Decl *D) const;

  /// True when the declaration is physically located in a system header.
  bool isInSystemHeader(const clang::Decl *D) const;

  // -- edges ---------------------------------------------------------------

  void addEdge(llvm::StringRef Kind, const std::string &Src,
               const std::string &Dst, clang::SourceLocation Loc,
               llvm::StringRef Flags = {}, int Weight = 1);

  /// Records declared-type relationships (return type, field type, base
  /// class, ...) by walking `T`'s structure down to named declarations.
  void addTypeEdges(const std::string &SrcUSR, clang::QualType T,
                    llvm::StringRef Kind, clang::SourceLocation Loc);

  /// Writes every buffered edge in first-seen order and clears the buffer.
  /// Ordering is deterministic so extractor output can be diffed in tests.
  void flushEdges();

  // -- misc ----------------------------------------------------------------

  void emitInclude(int FromFile, int ToFile, int Line, bool Angled,
                   llvm::StringRef Spelled);
  void emitDiag(llvm::StringRef Severity, clang::SourceLocation Loc,
                const std::string &Message);
  void emitMeta(llvm::StringRef Key, llvm::StringRef Value);

  /// Locations of `D` as (file id, line, column), or (-1, 0, 0).
  struct Loc {
    int File = -1;
    int Line = 0;
    int Col = 0;
  };
  Loc locOf(clang::SourceLocation L) const;

  const Options &opts() const { return Opts; }
  clang::ASTContext &ctx() const { return Ctx; }
  clang::SourceManager &sm() const;

  // -- statistics ----------------------------------------------------------
  struct Stats {
    unsigned long long Symbols = 0;
    unsigned long long Edges = 0;
    unsigned long long Includes = 0;
    unsigned long long TypeEdges = 0;
    unsigned long long UnresolvedCalls = 0;
    unsigned long long SyntheticIDs = 0;
  };
  const Stats &stats() const { return St; }
  Stats &stats() { return St; }

private:
  std::string computeUSR(const clang::Decl *D);
  std::string computeSyntheticUSR(const clang::Decl *D);

  FactWriter::Symbol buildSymbol(const clang::Decl *D, const std::string &USR) const;
  std::string buildFlags(const clang::Decl *D) const;
  static llvm::StringRef kindOf(const clang::Decl *D);
  std::string signatureOf(const clang::Decl *D) const;
  std::string typeTextOf(const clang::Decl *D) const;
  std::string parentUSROf(const clang::Decl *D);
  std::string accessOf(const clang::Decl *D) const;

  /// Shared implementation of declare()/reference(): `Full` selects whether a
  /// previously-stubbed node gets upgraded.
  const std::string &emitNode(const clang::Decl *D, bool Full);

  void addTypeEdgesImpl(const std::string &SrcUSR, clang::QualType T,
                        llvm::StringRef Kind, clang::SourceLocation Loc,
                        unsigned Depth, std::unordered_set<const void *> &Seen);

  FactWriter &W;
  clang::ASTContext &Ctx;
  const Options &Opts;

  std::unordered_map<const clang::Decl *, std::string> USRCache;
  /// USRs already emitted as full nodes in this TU.
  std::unordered_set<std::string> FullNodes;
  /// USRs emitted only as stubs so far.
  std::unordered_set<std::string> StubNodes;
  /// Redeclaration chains already collapsed onto one canonical Decl.
  std::unordered_map<std::string, const clang::Decl *> CanonicalDecl;

  struct EdgeKey {
    std::string Kind, Src, Dst;
    int File;
    bool operator==(const EdgeKey &O) const {
      return File == O.File && Kind == O.Kind && Src == O.Src && Dst == O.Dst;
    }
  };
  struct EdgeKeyHash {
    size_t operator()(const EdgeKey &K) const;
  };
  std::vector<FactWriter::Edge> Edges;
  std::unordered_map<EdgeKey, size_t, EdgeKeyHash> EdgeIndex;

  Stats St;
};

}  // namespace cg
