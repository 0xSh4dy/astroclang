# How Clang is used

`astroclang-index` is a Clang LibTooling program. This document records which parts of
Clang it uses, which it deliberately does not, and why each choice was made.

---

## 1. The choice, in one paragraph

Compilation database → `clang::tooling::ClangTool` → `FrontendAction` →
`RecursiveASTVisitor` over the `ASTContext` → a `PPCallbacks` hook for includes
and macros → a JSON Lines stream of facts. Every fact is read out of the
*semantically analyzed* AST: `CallExpr::getDirectCallee()`, `CXXMethodDecl::overridden_methods()`,
`CXXRecordDecl::bases()`, `QualType`. Nothing is inferred from a spelling.

---

## 2. Why LibTooling, and not the alternatives

### clangd

clangd is a language server. Its index (`clang::index::IndexData`, `Symbol`,
`Ref`, `Relation`) is built for interactive code navigation, and it is
structurally the wrong shape for this graph:

* **Its symbol identity is a name plus a USR, and its references are
  `(file, line, column)` triples with a role.** There is no containment
  hierarchy, no declaration/definition split in the node, and no edge kind that
  distinguishes a call from a base-class relationship.
* **Its index is produced by `index::IndexDataConsumer`, whose callbacks are
  `handleDeclOccurrence` and `handleMacroOccurrence`** — occurrence-shaped. The
  interesting facts here (`CXXRecordDecl::bases()`, `overridden_methods()`,
  `getDirectCallee()`) are not occurrences and would have to be reconstructed
  from the AST anyway, by which point clangd is a middleman adding a lifecycle.
* **It is a server.** Adopting it means adopting a file-watching, background-
  indexing, LSP-speaking process to answer questions this tool answers from a
  database. The failure modes become "the index is not ready yet" instead of
  "the index does not hold that".

The `clangIndex` *library* (USR generation) is used; clangd is not.

### ASTMatchers

Matchers are the right tool when the question is "find every node shaped like
this". They are a poor fit for "describe every declaration, with its
relationships", because:

* a matcher-based extractor needs one matcher per fact, and each match
  re-walks the AST;
* match callbacks give no natural place to maintain "the function I am
  currently inside", which every edge needs as its source;
* the matching DSL would sit between the code and the AST for no benefit —
  `RecursiveASTVisitor` with `TraverseDecl` maintaining a scope stack is
  shorter and does exactly what is needed.

`clangASTMatchers` was evaluated and is not linked, because nothing uses it:
the extractor visits the AST directly, and the tests assert on the fact stream
the extractor produces rather than on a matcher's view of the AST. Linking a
library to satisfy an argument in a document would be a strange reason to carry
a dependency.

### libclang (the C API)

Convenient, stable, and lossy. `CXCursor` gives a cursor kind and a spelling
but not the semantic relationships this graph is built from — no `QualType`
you can walk into named declarations, no override set, no instantiation
pattern. It would produce something close to a syntax-level index with better
locations, which is exactly what the tool exists not to be.

### Clang plugins / `clang -ast-dump=json`

`-ast-dump=json` is tempting because it needs no build integration: run the
compiler, parse the JSON. It is rejected because the dump is huge (hundreds of
megabytes for a real TU), unstable across versions, and carries no USRs by
default — so the identity problem, which is the whole point, comes straight
back.

---

## 3. The pipeline inside `astroclang-index`

```
main.cpp
  parseArgs                  --options, --print-config
  loadCompilationConfig      CompilationDatabaseFactory.cpp
  ClangTool(DB, {file})      clang::tooling::ClangTool
  Tool.run(AnalysisFactory)
      |
      +-- AnalysisAction.cpp  ASTFrontendAction
      |     createASTConsumer
      |       PPCallbacks  -> PreprocessorCollector   includes, macros
      |       ASTConsumer  -> AnalysisVisitor         everything else
      |
      +-- AnalysisVisitor.cpp  RecursiveASTVisitor<AnalysisVisitor>
      |     TraverseDecl           maintain the enclosing-symbol stack
      |     VisitCallExpr          resolved and indirect calls
      |     VisitDeclRefExpr       references
      |     TraverseLambdaExpr     attribute a body to its closure, not its creator
      |
      +-- Indexer.cpp         semantic model construction
            usrOf                identity
            declare/reference    node emission, stubs
            addEdge              edge emission, aggregated per file
            addTypeEdges         structural walk of a QualType
      |
      +-- FactWriter.cpp      JSON Lines serialization
```

### `ClangTool`, one file at a time

`ClangTool::run` takes a `CompilationDatabase` and a list of source files, and
for each one builds a `CompilerInstance` from the database's own arguments. The
driver invokes it with exactly one file, which keeps the process's memory
bound: the peak is one translation unit's AST, not the project's.

`Tool.run()` returns 1 on any diagnostic error. That return value is
**deliberately ignored**: a translation unit that failed to compile still has a
partial AST, and the declarations before the error point are real facts. The
error count goes into the fact stream instead (`diag` records and the `errors`
stats field), and the driver decides what a partial result is worth —
`index_project` stores it, keeps the diagnostics, and reports the file as
having errors rather than as failed.

### `libclangTooling` + `JSONCompilationDatabase`

`cg::loadCompilationConfig` (`CompilationDatabaseFactory.cpp`) tries, in order:

1. an explicit `--compdb` path,
2. a directory given with `-p/--compdb-dir`,
3. Clang's own search from the project root,
4. a **fallback database** for the single file, with conventional include
   directories and a language standard chosen from the file extension.

The fallback is a hand-written `CompilationDatabase` subclass rather than
`FixedCompilationDatabase`, so that it can report *why* it is being used
(`Heuristic = "no compilation database; flags were synthesised"`), and so that
the synthesized command line is the file's own rather than one command applied
to every file the tool is asked about. The driver carries that through to the
user: `get_index_status` reports accuracy as `exact`, `degraded` or `unknown`.

The distinction matters more in C++ than in almost any other language. A
missing `-I` does not produce a slightly worse index; it produces an index
where a header's declarations do not exist, and every call into them becomes a
stub. With `-std` wrong, `if constexpr` is a hard error and everything behind
it is missing — which is exactly what happened when this tool was first pointed
at googletest with `-std=c++14`, and why the evaluation records the standard it
used.

### `PPCallbacks` for includes and macros

`PreprocessorCollector::InclusionDirective` records the `FileEntry` the
preprocessor **actually opened**, after search-path resolution — not the string
written in the source. That distinction is why include-based impact analysis
works at all in C++: a header is reached through `-I` paths and relative
traversal that no amount of text matching reconstructs. An include that resolves
to nothing is counted and reported, because it is usually the first symptom of a
missing `-I` and it explains every symbol that goes missing downstream.

`MacroDefined` records macro definitions as symbols, with their parameters. A
macro *use* is deliberately not recorded as a call: after preprocessing, a
macro call leaves no trace in the AST, and what it expands to is not a call the
programmer wrote. Claiming otherwise would put edges in the graph that do not
correspond to anything in the source.

### `RecursiveASTVisitor`

`TraverseDecl` maintains a stack of the symbol currently being walked
(`Current`), which is the source of every `calls` and `references` edge. Two
things about it are worth noting:

* **Skipping a subtree must not materialize a node for its root.**
  `shouldTraverseInto` decides what is worth descending into; a declaration
  outside that set becomes a node only if something refers to it, through
  `reference()` at the point of use. That is both cheaper and more accurate: it
  records that something *in the project* depends on it.
* **A lambda body belongs to the closure, not to the function that created it.**
  `TraverseLambdaExpr` pushes the lambda's closure class as the current symbol,
  because the body runs when the closure is invoked — possibly in another
  function, another thread, or after the enclosing function has returned.
  Attributing its calls to the enclosing function would report a dependency that
  does not exist at the point claimed. The closure class is implicit, so this
  goes through `referenceSynthesized()` rather than `reference()`.

### `clang::index::generateUSRForDecl` for identity

See [`semantic-model.md`](semantic-model.md) §1. The one implementation detail
worth recording: the *canonical* declaration is used, because a redeclaration
shares its USR and that is what makes the cross-translation-unit merge work.

---

## 4. What is deliberately not indexed

Every one of these is a default in `cg::Options`, and each exists because
including it costs more than it explains.

| Option | Default | Why |
| --- | --- | --- |
| `--sys-headers` | off | A C++ TU pulls in tens of thousands of standard-library declarations that are identical in every project. Indexing them buries the project's structure. References to them are still *resolved* — as stubs — so `callers(std::vector<int>::resize)` is answerable |
| `--locals` | off | Function-local variables are numerous and rarely the subject of a question across translation units |
| `--params` | off | Parameters are visible in the signature that every function node already carries |
| `--template-instantiations` | off | Walking implicit instantiations is expensive on template-heavy code. The instantiation actually used at a call site is recorded either way, because call resolution happens in the non-template caller |
| `--implicit-decls` | off | Clang invents copy constructors and conversion operators by the thousand. They are not code anyone wrote |
| `--no-macros` | macros on | See above |
| `--max-type-depth` | 4 | Bounds the structural walk of a `QualType` |

The one thing this trades away is visible in the evaluation and is worth stating
plainly: **on a project like googletest, about half of all call edges land on a
stub** — a declaration the index knows by identity and location but whose body
it never analyzed. That is a deliberate default, not a failure, and
`docs/evaluation.md` reports it as a number rather than leaving it to be
discovered.

---

## 5. Why the analyzer is a separate process

* **Isolation.** A Clang front end can crash on adversarial input. A crash
  costs one translation unit's facts, not the index — and `index_project`
  keeps the previous entry for that file rather than replacing a complete
  result with a partial one.
* **Parallelism.** Translation units are independent by construction, so
  worker count is just process management. The measured speedup saturates at
  about 2.15×, and stops improving past four workers — see
  [`evaluation.md`](evaluation.md) §4 for the sweep, and for why the earlier
  figure of 3.10× was an artefact of the order the two runs happened in.
* **Testability.** The fact stream is JSON Lines on stdout, so a test asserts on
  exactly what the analyzer saw, with no database in the way. `python/tests/test_semantics.py`
  is built entirely on that.

The cost is one serialization pass and process startup per translation unit.
Measured against a front end that takes one to thirty seconds per file, it does
not appear in the numbers.
