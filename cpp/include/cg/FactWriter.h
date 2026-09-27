// Fact stream serialization.
//
// The extractor's only output is a stream of "facts" about one translation
// unit, encoded as JSON Lines.  Every fact is a flat object with a short "t"
// (type) discriminator.  Keeping the encoding flat and line-delimited means
// the stream can be produced incrementally, consumed with a streaming parser,
// and inspected with grep when a test disagrees with the tool.
//
// Records
// -------
//   {"t":"f",   "i":<int>, "p":<path>, "sys":<0|1>}
//   {"t":"sym", "u":<usr>, "k":<kind>, "n":<name>, "q":<qualified>, ...}
//   {"t":"edge","k":<kind>, "a":<usr>, "b":<usr>, ...}
//   {"t":"inc", "f":<file>, "b":<file>, "l":<line>, "ang":<0|1>}
//   {"t":"diag",...}, {"t":"meta",...}, {"t":"done",...}
//
// File paths are interned per translation unit and referenced by the small
// integer id from the "f" record; a large TU mentions the same headers
// thousands of times and re-emitting absolute paths dominates the byte count
// otherwise.
#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "llvm/ADT/StringRef.h"
#include "llvm/Support/raw_ostream.h"

namespace clang {
class FileEntry;
class SourceManager;
class SourceLocation;
}  // namespace clang

namespace cg {

/// Writes the fact stream for a single translation unit.
class FactWriter {
public:
  explicit FactWriter(llvm::raw_ostream &OS) : OS(OS) {}

  /// Interns a path, emitting its file record on first sight.
  /// Returns a small non-negative id used by every later reference.
  int internPath(llvm::StringRef Path, bool IsSystem);

  /// Interns the file a source location points into.  Returns -1 when the
  /// location is invalid or has no associated file (macro expansions and
  /// command-line buffers land here).
  int internLocation(const clang::SourceManager &SM, clang::SourceLocation Loc);

  /// Interns a FileEntry, honouring the system-header flag Clang tracked.
  int internFileEntry(const clang::FileEntry *FE, bool IsSystem);

  /// Path of an already-interned id; empty if unknown.
  const std::string &path(int Id) const { return Paths[Id]; }

  void emitMeta(llvm::StringRef Key, llvm::StringRef Value);
  void emitMetaRaw(llvm::StringRef Key, const std::string &RawJsonValue);

  /// Emits a declaration/definition node.  Fields are only written when set,
  /// which keeps the common case (a plain function) to a single short line.
  struct Symbol {
    std::string USR;
    llvm::StringRef Kind;
    std::string Name;
    std::string QualifiedName;
    std::string Signature;   // parameter list, for callables
    std::string TypeText;    // declared type / return type
    int File = -1, Line = 0, Col = 0;
    int EndLine = 0, EndCol = 0;
    int DefFile = -1, DefLine = 0, DefCol = 0;
    std::string ParentUSR;   // enclosing class/namespace/function
    std::string Flags;       // pre-rendered JSON object body, may be empty
  };
  void emitSymbol(const Symbol &S);

  /// Emits a directed, semantically resolved relationship.
  struct Edge {
    llvm::StringRef Kind;
    std::string Src;
    std::string Dst;
    int File = -1, Line = 0;
    int Count = 1;
    std::string Flags;  // pre-rendered JSON object body, may be empty
  };
  void emitEdge(const Edge &E);

  void emitInclude(int FromFile, int ToFile, int Line, bool Angled,
                   llvm::StringRef Spelled);
  void emitDiag(llvm::StringRef Severity, int File, int Line, int Col,
                const std::string &Message);
  void emitDone();

  unsigned fileCount() const { return static_cast<unsigned>(Paths.size()); }

  /// Number of include records written.  Counted here rather than by a caller
  /// because the preprocessor emits includes and the AST walk emits symbols;
  /// this is the one place both pass through.
  unsigned long long includeCount() const { return IncludeCount; }

  /// Appends `S` to `Out` as a quoted, escaped JSON string.
  static void writeJsonString(std::string &Out, llvm::StringRef S);

private:
  void flush(std::string &Line);

  llvm::raw_ostream &OS;
  unsigned long long IncludeCount = 0;
  std::vector<std::string> Paths;
  std::vector<char> IsSystem;
  std::unordered_map<std::string, int> PathIds;
};

}  // namespace cg
