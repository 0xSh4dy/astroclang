#include "cg/FactWriter.h"

#include "clang/Basic/FileEntry.h"
#include "clang/Basic/SourceLocation.h"
#include "clang/Basic/SourceManager.h"

namespace cg {

namespace {

/// Number of bytes in a UTF-8 sequence started by `Lead`, or 0 if `Lead` is
/// not a valid lead byte.
unsigned utf8SequenceLength(unsigned char Lead) {
  if (Lead < 0x80) return 1;
  if ((Lead & 0xE0) == 0xC0) return 2;
  if ((Lead & 0xF0) == 0xE0) return 3;
  if ((Lead & 0xF8) == 0xF0) return 4;
  return 0;
}

/// True when `Buf` holds a well-formed continuation of the sequence begun by
/// `Lead`.
bool validUtf8At(llvm::StringRef S, size_t I, unsigned Len) {
  for (unsigned K = 1; K < Len; ++K) {
    if (I + K >= S.size()) return false;
    unsigned char C = static_cast<unsigned char>(S[I + K]);
    if ((C & 0xC0) != 0x80) return false;
  }
  // Reject overlong encodings and surrogates so the emitted JSON is strictly
  // valid; a decoder downstream would otherwise reject the whole line.
  if (Len == 2) {
    unsigned char C = static_cast<unsigned char>(S[I]);
    if (C < 0xC2) return false;
  } else if (Len == 3) {
    unsigned char C0 = static_cast<unsigned char>(S[I]);
    unsigned char C1 = static_cast<unsigned char>(S[I + 1]);
    if (C0 == 0xE0 && C1 < 0xA0) return false;
    if (C0 == 0xED && C1 >= 0xA0) return false;  // surrogate half
  } else if (Len == 4) {
    unsigned char C0 = static_cast<unsigned char>(S[I]);
    unsigned char C1 = static_cast<unsigned char>(S[I + 1]);
    if (C0 == 0xF0 && C1 < 0x90) return false;
    if (C0 == 0xF4 && C1 >= 0x90) return false;
    if (C0 > 0xF4) return false;
  }
  return true;
}

void appendHex4(std::string &Out, unsigned Value) {
  static const char *Digits = "0123456789abcdef";
  Out += "\\u";
  Out += Digits[(Value >> 12) & 0xF];
  Out += Digits[(Value >> 8) & 0xF];
  Out += Digits[(Value >> 4) & 0xF];
  Out += Digits[Value & 0xF];
}

}  // namespace

void FactWriter::writeJsonString(std::string &Out, llvm::StringRef S) {
  Out += '"';
  for (size_t I = 0; I < S.size();) {
    unsigned char C = static_cast<unsigned char>(S[I]);
    switch (C) {
      case '"':  Out += "\\\""; ++I; continue;
      case '\\': Out += "\\\\"; ++I; continue;
      case '\b': Out += "\\b"; ++I; continue;
      case '\f': Out += "\\f"; ++I; continue;
      case '\n': Out += "\\n"; ++I; continue;
      case '\r': Out += "\\r"; ++I; continue;
      case '\t': Out += "\\t"; ++I; continue;
      default: break;
    }
    if (C < 0x20) {
      appendHex4(Out, C);
      ++I;
      continue;
    }
    if (C < 0x80) {
      Out += static_cast<char>(C);
      ++I;
      continue;
    }
    // Multi-byte: copy verbatim when well-formed, otherwise substitute the
    // Unicode replacement character so the line stays parseable.
    unsigned Len = utf8SequenceLength(C);
    if (Len > 1 && validUtf8At(S, I, Len)) {
      Out.append(S.data() + I, Len);
      I += Len;
    } else {
      Out += "\xEF\xBF\xBD";  // U+FFFD
      ++I;
    }
  }
  Out += '"';
}

int FactWriter::internPath(llvm::StringRef Path, bool System) {
  std::string Key = Path.str();
  auto It = PathIds.find(Key);
  if (It != PathIds.end()) {
    if (System) IsSystem[It->second] = 1;
    return It->second;
  }
  int Id = static_cast<int>(Paths.size());
  Paths.push_back(Key);
  IsSystem.push_back(System ? 1 : 0);
  PathIds.emplace(std::move(Key), Id);

  std::string Line = "{\"t\":\"f\",\"i\":";
  Line += std::to_string(Id);
  Line += ",\"p\":";
  writeJsonString(Line, Path);
  if (System) Line += ",\"sys\":1";
  Line += '}';
  flush(Line);
  return Id;
}

int FactWriter::internFileEntry(const clang::FileEntry *FE, bool System) {
  if (!FE) return -1;
  // tryGetRealPathName() resolves symlinks and `..`, so the same header
  // reached through two include paths interns to one id.
  llvm::StringRef Real = FE->tryGetRealPathName();
  if (Real.empty()) Real = FE->getName();
  return internPath(Real, System);
}

int FactWriter::internLocation(const clang::SourceManager &SM,
                               clang::SourceLocation Loc) {
  if (Loc.isInvalid()) return -1;
  // Macro arguments report the expansion site; the interesting file is where
  // the token was actually spelled.
  clang::SourceLocation Spelling = SM.getSpellingLoc(Loc);
  if (Spelling.isInvalid()) return -1;
  clang::FileID FID = SM.getFileID(Spelling);
  if (FID.isInvalid()) return -1;
  const clang::FileEntry *FE = SM.getFileEntryForID(FID);
  if (!FE) return -1;  // <built-in>, <command line>, <scratch space>
  return internFileEntry(FE, SM.isInSystemHeader(Spelling));
}

void FactWriter::flush(std::string &Line) {
  Line += '\n';
  OS << Line;
  Line.clear();
}

void FactWriter::emitMeta(llvm::StringRef Key, llvm::StringRef Value) {
  std::string Line = "{\"t\":\"meta\",\"k\":";
  writeJsonString(Line, Key);
  Line += ",\"v\":";
  writeJsonString(Line, Value);
  Line += '}';
  flush(Line);
}

void FactWriter::emitMetaRaw(llvm::StringRef Key, const std::string &RawJson) {
  std::string Line = "{\"t\":\"meta\",\"k\":";
  writeJsonString(Line, Key);
  Line += ",\"v\":";
  Line += RawJson;
  Line += '}';
  flush(Line);
}

void FactWriter::emitSymbol(const Symbol &S) {
  std::string Line = "{\"t\":\"sym\",\"u\":";
  writeJsonString(Line, S.USR);
  Line += ",\"k\":";
  writeJsonString(Line, S.Kind);
  Line += ",\"n\":";
  writeJsonString(Line, S.Name);

  if (!S.QualifiedName.empty() && S.QualifiedName != S.Name) {
    Line += ",\"q\":";
    writeJsonString(Line, S.QualifiedName);
  }
  if (!S.Signature.empty()) {
    Line += ",\"s\":";
    writeJsonString(Line, S.Signature);
  }
  if (!S.TypeText.empty()) {
    Line += ",\"ty\":";
    writeJsonString(Line, S.TypeText);
  }
  if (S.File >= 0) {
    Line += ",\"f\":";
    Line += std::to_string(S.File);
    if (S.Line > 0) {
      Line += ",\"l\":";
      Line += std::to_string(S.Line);
      Line += ",\"c\":";
      Line += std::to_string(S.Col);
    }
  }
  if (S.EndLine > 0) {
    Line += ",\"el\":";
    Line += std::to_string(S.EndLine);
    Line += ",\"ec\":";
    Line += std::to_string(S.EndCol);
  }
  if (S.DefFile >= 0) {
    Line += ",\"df\":";
    Line += std::to_string(S.DefFile);
    Line += ",\"dl\":";
    Line += std::to_string(S.DefLine);
    Line += ",\"dc\":";
    Line += std::to_string(S.DefCol);
  }
  if (!S.ParentUSR.empty()) {
    Line += ",\"p\":";
    writeJsonString(Line, S.ParentUSR);
  }
  if (!S.Flags.empty()) {
    Line += ",\"F\":{";
    Line += S.Flags;
    Line += '}';
  }
  Line += '}';
  flush(Line);
}

void FactWriter::emitEdge(const Edge &E) {
  std::string Line = "{\"t\":\"edge\",\"k\":";
  writeJsonString(Line, E.Kind);
  Line += ",\"a\":";
  writeJsonString(Line, E.Src);
  Line += ",\"b\":";
  writeJsonString(Line, E.Dst);
  if (E.File >= 0) {
    Line += ",\"f\":";
    Line += std::to_string(E.File);
    if (E.Line > 0) {
      Line += ",\"l\":";
      Line += std::to_string(E.Line);
    }
  }
  if (E.Count > 1) {
    Line += ",\"w\":";
    Line += std::to_string(E.Count);
  }
  if (!E.Flags.empty()) {
    Line += ",\"F\":{";
    Line += E.Flags;
    Line += '}';
  }
  Line += '}';
  flush(Line);
}

void FactWriter::emitInclude(int FromFile, int ToFile, int Line, bool Angled,
                             llvm::StringRef Spelled) {
  std::string Line = "{\"t\":\"inc\",\"f\":";
  Line += std::to_string(FromFile);
  Line += ",\"b\":";
  Line += std::to_string(ToFile);
  Line += ",\"l\":";
  Line += std::to_string(Line);
  if (Angled) Line += ",\"ang\":1";
  Line += ",\"sp\":";
  writeJsonString(Line, Spelled);
  Line += '}';
  flush(Line);
}

void FactWriter::emitDiag(llvm::StringRef Severity, int File, int Line, int Col,
                         const std::string &Message) {
  std::string L = "{\"t\":\"diag\",\"sev\":";
  writeJsonString(L, Severity);
  if (File >= 0) {
    L += ",\"f\":";
    L += std::to_string(File);
    L += ",\"l\":";
    L += std::to_string(Line);
    L += ",\"c\":";
    L += std::to_string(Col);
  }
  L += ",\"m\":";
  writeJsonString(L, Message);
  L += '}';
  flush(L);
}

void FactWriter::emitDone() {
  std::string Line = "{\"t\":\"done\",\"files\":";
  Line += std::to_string(Paths.size());
  Line += '}';
  flush(Line);
}

}  // namespace cg
