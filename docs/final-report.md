# Final report

`astroclang` is a semantic index of C and C++ projects, and an MCP server
that answers questions about one without making an agent read the source. This
document is the account of what was built and what was learned building it.
Every claim here is backed by a document that says more, and every number by a
script that produced it.

```sh
astroclang index /path/to/project
astroclang mcp   /path/to/project
```

---

## 1. Architecture

```
                C/C++ repository
                       |
             compile_commands.json          discovery.py
                       |
                       v
                  astroclang-index                 cpp/  (C++17, Clang LibTooling)
                 one TU, one process
                       |
                  JSONL facts on stdout
                       |
                       v
               sqlite semantic store           store.py
             raw_* per TU  +  merged symbol
                       |
              +--------+--------+
              |                 |
              v                 v
        query.py (graph)    changes.py (git diff)
              |                 |
              +--------+--------+
                       |
                       v
               tools.py  ->  mcp_server.py
                       |
                       v
                 coding agents
```

The boundaries between those stages are the design, and each one is there for a
reason worth being able to state.

**`astroclang-index` is a process, not a library.** It is given one translation unit
and the compiler arguments for it, and writes JSON Lines to stdout. It never
opens a database and holds no state between translation units. Three things
follow: a Clang front end that crashes on adversarial input costs one
translation unit's facts rather than the index; translation units are
independent by construction, so parallelism is just process management; and the
output is text, so a test asserts on exactly what the analyzer saw with no
database in the way. `python/tests/test_semantics.py` is built entirely on
that last property.

**The fact stream is the contract.** Nothing between the analyzer and the store
interprets C++. The store reads a documented record format, so the analyzer is
replaceable — a second language would be a second analyzer writing the same
records, not a rewrite. That is the extensibility the specification asked for,
and it is bought by keeping policy out of the analyzer.

**The store has two layers.** Raw facts are kept per translation unit, exactly
as reported, so dropping a translation unit is a `DELETE` and not a
reconstruction. The merged `symbol` table is derived, so it can always be
thrown away and rebuilt — which is what makes the merge rules safe to change,
because they are code that runs over data rather than data itself.

**Everything above the store is questions.** `query.py` owns the graph
semantics, `tools.py` owns the answer shape, `mcp_server.py` owns the transport. An
agent asking "who calls this" travels the whole stack; a test asserting on a
resolution travels the same one, minus the transport.

More: [`architecture.md`](architecture.md).

---

## 2. What was learned from `code-review-graph`, and what was rejected

`code-review-graph` was studied as a reference and not touched: its working
tree is clean at the end of this work, and no part of it was modified, forked
or extended. The full analysis is [`reference-analysis.md`](reference-analysis.md);
this is the summary.

**Adopted.** (a) A graph of typed nodes and edges rather than a symbol table,
because the questions agents ask are relational. (b) A persistent index built
ahead of the questions, so an agent does not pay for parsing. (c) Token
discipline as a design constraint rather than an afterthought — the shape of an
answer is part of the interface. (d) Progressive disclosure: repository → file
→ symbol → relationships → source region, with source only on request. (e) A
diff-driven entry point, because that is when an agent actually needs the
graph.

**Rejected, and why.** (a) Its language model is text-shaped, and deliberately
so: it parses with tree-sitter, emits a call edge carrying whatever callee
*text* the syntax tree offered, and then runs a cascade of passes trying to
upgrade that name to something real — same-file lookup, then a graph-wide
bare-name match accepted only when exactly one candidate qualifies. That is a
reasonable design for a dynamically typed language and a poor one for C++,
where the same spelling names several distinct entities and the same entity has
several spellings. It is also the specific thing this project exists to replace:
`obj.resize(100)` must resolve to a declaration, not to the string `resize`.
So identity here is a Clang USR computed from the canonical declaration, and
overloads, namespaces, classes and template specializations are distinct nodes
by construction rather than by a matching heuristic. The tool ships a much
larger language list than this one — and for C and C++ its extraction is a
handful of syntax-node names (`class_specifier`, `function_definition`) with no
type information behind them at all. (b) Its storage
was arranged around whole-repository rescans; here the raw layer is per
translation unit precisely so that incremental update is a delete and an
insert. (c) Its tool surface returned more prose per answer; the tools here
return counts where a count is the answer and a `file:line` as the handle for
the next question.

**What was kept from it without change:** nothing. Not a line of its
implementation was copied.

---

## 3. How Clang is used

`astroclang-index` drives **Clang LibTooling** — `clang::tooling::ClangTool` over a
`CompilationDatabase` — rather than clangd, and rather than shelling out to
`clang -ast-dump`. The reasoning, in short:

* **clangd is a language server, not an index.** Its background index is
  designed to answer "where is this symbol defined" for an editor. It does not
  publish resolved call graphs, and its index is not a queryable artifact with
  a schema this tool controls. Using it would mean parsing its serialized index
  format and accepting its notion of what a relationship is.
* **LibTooling gives the AST, the `SourceManager` and the preprocessor in one
  pass.** The graph needs all three: the AST for declarations and calls, the
  `SourceManager` for the include graph and for spelling locations, the
  preprocessor for macro definitions.
* **RecursiveASTVisitor, not ASTMatchers.** Matchers are the right tool for
  finding patterns; this tool visits everything, and a visitor that tracks its
  enclosing declaration is simpler than a match callback that does.

What is used, concretely:

| Need | Mechanism |
| --- | --- |
| Identity | `clang::index::generateUSRForDecl` on the canonical declaration |
| Overload resolution | already done — `CallExpr::getDirectCallee()` is the resolved declaration |
| Virtual dispatch | `CXXMethodDecl::isVirtual()` / `isPure()` become edge flags |
| Overrides | `CXXMethodDecl::overridden_methods()` |
| Inheritance | `CXXRecordDecl::bases()` |
| Templates | `getSpecializedTemplate()`, `getTemplateInstantiationPattern()` |
| Types | a structural walk of `QualType` (`addTypeEdges`) |
| Includes | `PPCallbacks::InclusionDirective`, after search-path resolution |
| Macros | `PPCallbacks::MacroDefined` |
| Compiler arguments | `compile_commands.json`, with `--print-config` to inspect them |

The visitor's traversal rule matters more than any single call: **a
declaration is only materialized when something in the project refers to it.**
Every header a translation unit includes contributes thousands of top-level
declarations, and none of them is part of the project. One that is genuinely
referenced becomes a node at the point of use, which is both cheaper and more
accurate — it records that something in the project depends on it.

More: [`clang-usage.md`](clang-usage.md).

---

## 4. The semantic model

Nodes are symbols. The kinds are `function`, `method`, `constructor`,
`destructor`, `conversion_function`, `class`, `struct`, `union`, `enum`,
`enumerator`, `field`, `variable`, `typedef`, `namespace`, `namespace_alias`,
`template_parameter`, `macro`, and `unknown` for anything the classifier does
not recognize. `class` and `struct` are Clang's own `KindName` rather than a
choice made here, so a `class` stays a `class` and a `struct` stays a `struct`
in the answer. A function template is a `function`: the template and its
specializations are distinguished by the `instantiates` and `specializes`
edges and by flags, not by a separate kind.

Every symbol carries a USR, a name, a qualified name, a signature, a kind, a
source location, a definition location when it differs, flags (`virtual`,
`pure`, `override`, `const`, `static`, `using`, …), and its lexical parent.

Thirteen edge kinds:

| Edge | Meaning |
| --- | --- |
| `calls` | a resolved call — the exact overload the compiler selected |
| `calls_indirect` | a call through a function pointer, member pointer or `std::function`, resolved to the *variable* |
| `references` | a non-call use: a read, a write, an address taken |
| `contains` | containment: namespace → class → field/method |
| `inherits` | a class derives from a base |
| `overrides` | this method overrides that one |
| `specializes` | this declaration specializes that template |
| `instantiates` | this instantiation came from that pattern |
| `returns`, `param_type` | the function's return type, a parameter's type |
| `field_type`, `var_type` | a field's or variable's type |
| `aliases` | a typedef and the type it names |

Two decisions in the type walk are where the interesting bugs were, and both
are now stated in the model document because both are places where the obvious
implementation is quietly wrong:

**A name the author wrote outranks what it expands to.** `getAs<T>` desugars
before it answers. Asked in the wrong order, a field of type `clib_visit_fn`
comes back as a `clib_point`, because that is what the callback's signature
mentions. The typedef is now tested first and wins.

**The cycle guard is keyed on the type node, not its canonical form.** Those
differ exactly where the walk does its work — a type written in source is
usually sugar over the declaration it names. Keying on the canonical form
inserted the `RecordType` at entry, and the unwrapping step then met the same
`RecordType`, was rejected as a cycle, and emitted *no edge at all*.

More: [`semantic-model.md`](semantic-model.md).

---

## 5. Storage

SQLite, two layers, one file per project at `.astroclang/index.db`.

**Raw layer** — `raw_symbol`, `raw_edge`, `raw_include`, `raw_diag`, keyed by
translation unit. Exactly what each unit reported, including its own report of
a shared header. This is what makes incremental update a `DELETE`: re-indexing
one file removes its rows and inserts replacements, and nothing else moves.

**Merged layer** — one `symbol` table, rebuilt by `REBUILD_SYMBOLS`, which
picks a representative row per USR with `ROW_NUMBER() OVER (PARTITION BY usr
ORDER BY ...)`. It is derived, so it can be discarded and recomputed at any
time; that is what makes the merge rules safe to change.

Why SQLite rather than an embedded graph database or a bespoke format: the
questions are traversals over an adjacency list with a stable key, which is
what a relational index with the right indexes does well; the file is a single
portable artifact; and it is in the standard library, so the tool has no
dependencies to install.

The per-translation-unit raw layer is what makes dropping a unit cheap. Two
things this document previously credited with making queries fast are worth
correcting here, because both were asserted rather than measured and neither
survives re-measurement: planner statistics, which the earlier text said took a
query "from 53 ms to 0.02 ms" — the current schema shows no difference between
having them and not — and the file-table caches, whose real saving is 0.68 ms
on `find_symbol` (1.23 ms to 0.55 ms) and nothing at all on the two queries
that dominate a hub. `ANALYZE` is still run, because `REBUILD_SYMBOLS` is a
six-way join over the whole raw layer and that is the kind of plan statistics
genuinely inform; `architecture.md` §3.4 has the numbers for both.

More: [`architecture.md`](architecture.md) §3.4, and the measured sizes in
[`evaluation.md`](evaluation.md) §3.

---

## 6. The MCP interface

Nineteen tools over JSON-RPC on stdio, one JSON message per line. Nothing but
protocol goes to stdout; diagnostics go to stderr.

| Tool | Answers |
| --- | --- |
| `get_index_status` | what is indexed, and how far to trust it |
| `find_symbol` | a name → the symbols it could mean |
| `get_symbol`, `get_function`, `get_class` | one symbol, in the detail its kind deserves |
| `search_symbols` | substring search over names |
| `get_callers`, `get_callees`, `get_references` | who reaches this, and how |
| `get_inheritance` | bases, derived, overrides |
| `get_symbol_dependencies` | outgoing relationships, grouped by edge kind |
| `get_file`, `get_file_symbols`, `get_includes`, `get_file_dependencies` | a file without reading it |
| `get_source_context` | a small region around a symbol |
| `get_impact_analysis` | what a change could affect, by degree |
| `get_changed_symbols` | what a diff changed, in symbols |
| `get_diagnostics` | where the analysis could not see the whole unit |

Four rules shape every answer, and they exist because the reader pays for every
token and cannot see the repository:

1. **Nothing returns a source file.** `get_source_context` returns a padded
   region with line numbers, and it is the only tool that returns text.
2. **Every symbol is named `file:line`, and that spelling resolves exactly when
   passed back.** An agent follows one answer to the next question without ever
   handling a USR.
3. **A cut list says so and carries its true length.** `{"callers": [10 of
   them], "caller_count": 3606}` — exact, and costing one indexed query.
4. **Nothing is guessed.** A name denoting three overloads comes back as three
   candidates; a dependency that holds only under run-time dispatch is labelled
   `possible` and says why.

More: [`mcp.md`](mcp.md), which documents every argument and shows real output.

---

## 7. Incremental indexing

```
git change  →  changed files  →  affected translation units
            →  re-index those  →  REBUILD_SYMBOLS  →  updated graph
```

Detection is by a SHA-256 of the file's own bytes plus the compilation
database's mtime, and a header's change invalidates every translation unit that
includes it — which is what the include graph is for.
Re-indexing a unit deletes exactly its raw rows and inserts replacements; the
merged layer is then recomputed over what remains. Measured cost in
[`evaluation.md`](evaluation.md) §7.

`get_changed_symbols` closes the loop from the other end: it takes a revision,
a range or the working tree, attributes each changed line range to the
**innermost symbol that contains it**, reports the enclosing chain in `within`,
and counts lines that fall inside nothing rather than dropping them — a diff
over an unseen file would otherwise appear to have changed nothing at all.

---

## 8. Tests

**333 tests** across nine files, all of which run without a compiler except the
semantic ones, which drive the real extractor over a C and C++ corpus.

| File | Tests | What it holds down |
| --- | ---: | --- |
| `test_semantics.py` | 78 | semantic resolution, over the corpus, through the real extractor |
| `test_tools.py` | 71 | the answer shape: cuts, refusals, and notes |
| `test_query.py` | 52 | the graph: traversal, folding, scoping |
| `test_cli.py` | 28 | the command line |
| `test_indexer.py` | 28 | discovery, planning, incremental update |
| `test_mcp.py` | 27 | the protocol: framing, schemas, errors |
| `test_store.py` | 21 | schema, merge rules, migration |
| `test_changes.py` | 16 | diff → symbols → impact, against real git repositories |
| `test_facts.py` | 12 | the fact-stream format |

Counted by the loader rather than by `grep def test`: 327 distinct method names,
six of which are used twice, for 333 collected tests. Four of those six are the
same protocol behaviour asserted twice in `test_mcp.py` — once with an index and
once without one, which is a difference worth two tests rather than one. An
earlier draft of this table said 318 and summed to 318; that was `grep`'s count,
and `grep` is not what runs the suite.

The corpus (`python/tests/corpus/`, 473 lines across four translation units and
two headers) is deliberately small and adversarial rather than large and
representative — every construct in it is there because some resolution rule
had to be pinned down, not because a real project would contain it.

**C** (`c_lib.h`, `c_lib.c`, `c_main.c`):

| Construct | What the test pins down |
| --- | --- |
| functions, declarations vs definitions | a call resolves to the definition when the TU holds it and to a stub when it does not |
| structs, and an anonymous struct inside a `typedef` | two symbols sharing a display name, distinguished by USR and reported as two candidates |
| typedefs, including a function-pointer typedef | the alias edge names the typedef the author wrote, not what it expands to |
| enums and their enumerators | an enumerator is its own node inside the enum that contains it |
| globals | a variable read is a `references` edge, a variable called through is `calls_indirect` |
| function pointers | a call through one is recorded against the variable, and the address-taken functions as references — never as a resolved call |
| `static` functions | internal linkage: a `static` helper is not the same entity as a same-named one in another translation unit |
| preprocessor macros, function-like and object-like | a macro node exists at its definition and no edge reaches it; the test asserts that no edge of any kind does |
| includes, project and system | the include graph distinguishes them |

**C++** (`shapes.h`, `shapes.cpp`, `usage.cpp`):

| Construct | What the test pins down |
| --- | --- |
| namespaces, including a class nested in one | fully qualified names, and that the enclosing namespaces are part of a symbol's identity |
| classes and structs | `class` and `struct` reach the answer as Clang's own kind name |
| single, multiple and virtual inheritance | base and derived edges, direct and transitive, with access |
| virtual functions, pure virtuals, overrides | a call resolves to the *static* declaration; the override appears under `possible` with a reason |
| overloaded functions and methods | each overload is a distinct node and each call site picks the one the compiler chose |
| templates: class, function, and their instantiations | an instantiation links to its pattern and a specialization to its template, so a change to the template reaches users who only wrote the instantiation |
| constructors and destructors | a constructor call is a call, not a declaration |
| `using` aliases and typedefs | the alias is its own symbol and points at what it names |
| nested classes | containment through the enclosing class |
| static methods | containment without an instance |
| overloaded operators (`operator+=`) | an operator is a method with a name, and resolves like any other call |
| lambdas | the body's calls are attributed to the closure, not to the function that wrote it |
| references and pointers | a parameter's type is the pointee; a call through a pointer is indirect |
| `auto` | the deduced type, not the placeholder |
| header/source splits | a declaration's definition location is reported separately from its declaration location |

The tests assert **resolution**, not the presence of syntax nodes: not "there
is a call edge from here" but "this call resolves to that exact overload, in
that class, at that line".

---

## 9. Performance

Measured on the machine described in [`evaluation.md`](evaluation.md) §1 — a
laptop with a desktop session running, which is the honest context for every
number below and the reason the spread is large.

**Indexing** (`/usr/src/googletest`, 108 translation units of C++17):

| | |
| --- | --- |
| Translation units | 108 |
| Failed | 0 |
| Wall time | 124.5 s at `jobs=8`; 266.2 s serial — the sweep is in `evaluation.md` §4 |
| Seconds per translation unit | 1.15 |
| Peak per-unit RSS | 1.86 GB — and 1.84 GB of it is the Clang front end, not this tool |
| Database size | 570.3 MB, 55 725 symbols, 910 451 edges |

**Query latency**, over 200 sampled symbols: medians of 0.22–0.64 ms and
payloads of half a kilobyte, which is the number that matters — an agent asking
about an ordinary symbol pays about half a millisecond and about 500 bytes. The
worst case — the single most-called symbol in the index, where the answer
carries an exact `COUNT(DISTINCT src)` — is reported separately and is honestly
slow: 25 ms for `get_symbol`, 56 ms for `get_callers`, both measured six sweeps
deep because the first figures published for them did not reproduce.
`evaluation.md` §5 has the table.

**Scaling** is the finding worth carrying forward, and it is not the finding an
earlier draft of this report claimed. The speedup **saturates at about 2.15×**
on this machine's four physical cores, and eight workers are no faster than
four:

| `jobs` | seconds | speedup | peak concurrent extractor RSS | swap growth |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 266.19 | 1.00× | 1850.1 MB | +20 MB |
| 2 | 175.53 | 1.52× | 2062.4 MB | +211 MB |
| 4 | 122.97 | 2.16× | 2240.2 MB | +155 MB |
| 8 | 124.54 | 2.14× | 2437.3 MB | 0 MB |

I had written that eight concurrent front ends "drive the machine into swap".
The sweep does not support that: peak concurrent RSS reaches 2.44 GB of the
machine's 15.8 GB, and swap growth is between zero and 211 MB. **Memory is not
the binding constraint here** — the knee is at four workers, which is the
physical core count, and the default worker count of `min(32, cpu_count())`
over-provisions on an SMT machine. The earlier claim of a 3.10× speedup was an
artefact of a cold page cache on the serial run; `evaluation.md` §4 records
both corrections.

---

## 10. Limitations

Summarized here; [`limitations.md`](limitations.md) is the full account.

* **Analysis is per translation unit**, so a declaration defined in another unit
  is a stub with no body. On googletest about half of all call edges land on
  one. This is deliberate — whole-program analysis costs a link step's worth of
  memory on an already memory-bound front end — and it is measured rather than
  hidden.
* **Run-time dispatch is labelled, never resolved.** A virtual call resolves to
  the static declaration; overrides appear under `possible` with a reason. A
  call through a function pointer is recorded against the variable.
* **Macro uses are not recorded.** A `#define` is a node with a location and
  nothing points at it. Refusing to invent a `calls` edge to a macro is
  deliberate and tested; that an empty reference list then reads as "unused" is
  not, and both tools now say which it is.
* **A function-pointer typedef's `aliases` edge names the types in its
  signature**, because there is no declaration for the pointer type itself to
  point at. It is a *mentions* relationship wearing the name of an *is*.
* **An anonymous struct inside a typedef shares the typedef's display name**,
  so a raw edge list shows what looks like a self-edge. The USRs differ and
  `find_symbol` returns both as candidates.
* **Nothing is claimed about other languages**, and none is planned for the
  sake of completeness.

---

## 11. Git history

Every commit here is a working checkpoint: the build passes and the tests that
existed at that point pass. The order is the order the work actually happened,
including the corrections.

```
011fc3a chore: project scaffolding and reference analysis
4451eef feat(cpp): add clang semantic extractor emitting JSONL facts
30b93d9 feat(cpp): mark stub symbols in the fact stream
8ea50de fix(cpp): attribute a lambda body's calls to the lambda, not its creator
e122fa7 feat(store): add the fact-stream reader and the sqlite semantic store
2647ba1 feat(index): discover compilation databases and index incrementally
eb364da feat(query): add the semantic query layer
570ffd6 fix(cpp): name symbols, types and linkage the way a reader means them
f176467 fix(query): fold relationships reported by every translation unit
6beb803 test(cpp): add a corpus asserting semantic resolution
9881746 fix(index): put a symbol where it actually is
d126cd6 feat(query): return a small source region around a symbol
998c108 fix(store): keep the definition when merging a header's reports
c46e130 feat(git): say which symbols a diff changed
11b247a fix(query): count calls, not translation units
524b5f6 feat(tools): expose the index as questions an agent can ask
5484826 feat(mcp): serve the tools to a coding agent over stdio
cb49585 feat(cli): build the index and ask it questions from a shell
b725a8d perf(query): measure a real project, and fix what it showed
496929f build(cpp): drop a Clang library nothing links against
0e3aa30 feat(scripts): generate a compilation database for a tree without one
8231606 fix(tools): say when a list was cut, and how long it really was
25452e3 perf(eval): measure a sample of symbols, and report resolution honestly
c014bff docs: describe the architecture, the model, the interface and the cost
3ffa9b6 fix(cpp): record the type a declaration names, not what it desugars to
8c94bf6 fix(tools): count a cut dependency group by its true total
1d1d167 fix(query): make impact analysis reach beyond calls
e422969 fix(tools): say that a macro's empty answer means "not tracked"
87f14ee docs: show the third caller get_callers returns
664e2bc docs: add a top-level README
ccda648 docs(eval): re-run the evaluation after the type-edge fix
25129b7 docs: correct the scaling and cache claims that quoted it
f9d29cb docs: add the final report
798e40e docs: fix the commit count and the claims that drifted from it
```

The list ends at `798e40e`. The two commits after it corrected this section —
the count, and then this paragraph — and they are not in the list for the
obvious reason: a list of commits cannot contain the commit that writes it.

A second line of work ran alongside this one and met it in a merge. It was
working the same failure from the other side — the same truncation audit, in
the same tools — so its four code commits are more of the story above rather
than a separate one:

```
dc829f2 fix(tools): finish the truncation audit the first pass started
d8d1713 fix(query): resolve a symbol by its definition, and parse a span
e1169f5 perf(eval): script the worker-count sweep, and stop citing the withdrawn number
9a536e9 fix(tools): page the type-edge lookup like every other impact bucket
0edd2ea Merge master into worktree-paging-audit
```

That line also wrote a README and a final report of its own, and corrected
their counts, in four further commits. The merge resolved every document both
lines touched in favour of this one — the copy whose numbers were measured
last — so those four are superseded rather than lost.

An earlier draft said twenty-nine commits. That was true when it was drafted and
false by the time it was committed, which is the same failure as every other
number corrected in this document, and the reason the total is no longer stated
here. A count that has to be rewritten every time it is checked is not a
measurement; it is a trap this section kept walking into.

The last nine are the ones worth reading, and they are two halves of one story.
The first four of them fixed a feature that was **silently producing nothing** —
type edges absent from the graph, an impact bucket that was always empty, a
count that under-reported exactly when it was cut, an empty list that meant
"unchecked". The fifth is the same failure in documentation: `get_callers` was
shown returning two callers where the index had three, and a reader would have
had no way to tell.

Then, having found four of those, the evaluation itself had to be re-run — and
it turned out to have the same disease. A speedup of 3.10×, a query that "went
from 53 ms to 0.02 ms", a worst case of 143 ms: all three were asserted rather
than measured, all three appeared in more than one document, and none of them
survives being measured properly. `ccda648` replaces them with the numbers the
scripts actually produce, including the ones that contradict the story this
project had been telling about its own scaling.

None of them raised an error, failed a test, or looked wrong in an answer: a
graph with no `field_type` edges reads exactly like a project with no fields,
and an impact analysis with no indirect callers reads exactly like a symbol that
has none. Every one of them was found by asking the tool a question whose answer
was already known and checking the answer against it — not by a test, because a
test asserts the shape it was written to assert.

That is the lesson this project would carry into the next one. For a tool whose
whole value is that an agent can trust an answer it did not verify, an empty
answer is the most dangerous thing it can give — and the defence is not more
tests over the same shapes, but a query that treats "nothing here" as a claim
requiring evidence.
