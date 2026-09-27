# Final report

The project as a whole: what was built, what it is made of, what it costs, what
it does not do, and how it got here. Each section states its conclusion and
points at the document that argues it.

---

## 1. Architecture

```text
                C/C++ repository
                       │
                       ▼
              compilation database          discovery.py
                       │
                       ▼
                 Clang analysis             cg-index  (C++, LibTooling)
                       │
                       ▼
                 semantic index             store.py  (SQLite)
                       │
              ┌────────┴────────┐
              ▼                 ▼
        graph / queries      git diff
          query.py          git.py, changes.py
              └────────┬────────┘
                       ▼
                   MCP server               mcp_server.py
                       │
                       ▼
                 coding agents
```

The pipeline is split across a process boundary, and that is the load-bearing
decision. `cg-index` is a short-lived C++ binary that analyzes **one**
translation unit and writes a stream of facts to stdout. Everything else is
Python: it decides what to analyze, runs extractors, merges their facts into a
database, and answers questions from it.

The boundary exists because Clang's AST is per-translation-unit, in-memory and
C++, while the graph is cross-translation-unit, persistent and merged. One
process doing both must either hold every AST at once or re-parse to answer a
question. Split, an extractor dies after each file and leaves facts behind, so
the index can answer questions about a project nobody currently has loaded. It
also means a front-end crash costs one file's facts rather than the index, and
that the fact stream is testable without a database in the way.

| Component | Owns |
| --- | --- |
| `discovery.py` | what to analyze, with what flags, and how confidently |
| `indexer.py` | running extractors, in parallel, incrementally |
| `facts.py` | the wire format between the two halves |
| `store.py` / `schema.py` | the persistent index |
| `query.py` | questions over the merged graph |
| `git.py` / `changes.py` | a diff, expressed as symbols |
| `tools.py` | the named questions and the shape of their answers |
| `mcp_server.py` / `cli.py` | the two ways in |

→ [`architecture.md`](architecture.md) §2–3, and §4 for the alternatives each
decision beat.

---

## 2. Reference analysis

`code-review-graph` was studied as a reference implementation and **not
modified**; it is a separate repository and remains untouched. The full
analysis is [`reference-analysis.md`](reference-analysis.md).

**Adopted.** Three ideas carry over almost unchanged, because they are about
serving agents rather than about any particular language:

* **Progressive disclosure.** An answer names symbols and locations; source is
  fetched only when asked for. This is what makes the tool worth its token cost
  and it is the organising principle of the MCP surface.
* **A query layer distinct from the store.** Questions are named operations
  over the graph, not SQL leaking into tool handlers.
* **Tools declared as data.** Each tool is a record — name, arguments, handler,
  rendering — so the MCP schema, the CLI and the documentation cannot drift
  apart from one another.

**Rejected, and why.**

* **Name-based node identity.** The reference identifies symbols largely by
  textual name, which is workable where a name denotes one thing. In C++ it
  does not: `foo(int)` and `foo(float)`, `A::foo` and `B::foo`, and a template
  and its instantiations are different entities. Identity here is Clang's USR.
* **Parser-per-language with a shared graph model.** Reasonable as a design and
  wrong as a priority: the interesting C++ questions are answered by semantic
  analysis that no tree-sitter grammar performs, and building for a second
  language before the first is good would have been a distraction. The
  analyzer is replaceable in principle; no second one was written.
* **File-level granularity for impact.** A changed file is not a unit a
  reviewer thinks in. Changes are attributed to the innermost symbol
  containing them, with the enclosing chain kept alongside.

---

## 3. C/C++ analysis

The extractor is built on **Clang LibTooling**, driving the same front end that
would compile the code. It walks the real AST after semantic analysis, so
overload resolution, template instantiation and type deduction have already
happened and the answers are the compiler's rather than a guess at them.

| API | Used for |
| --- | --- |
| `ClangTool` + `JSONCompilationDatabase` | compiling each file with its own recorded flags |
| `RecursiveASTVisitor` | walking declarations, references and call sites |
| `clang::index::generateUSRForDecl` | stable identity for every symbol |
| `PPCallbacks` | includes as the preprocessor saw them, and macro definitions |
| `SourceManager` | spelling and expansion locations, so a location is where a reader would look |

**Why not the alternatives.** `clangd` was rejected: its index is optimised for
interactive completion, discards what it does not need for that, and offers no
supported way to ask the questions this tool exists to ask. ASTMatchers are a
good fit for finding patterns and a poor one for exhaustively walking a TU and
emitting facts about everything. `libclang`'s C API does not expose enough of
the AST. `-ast-dump=json` is a serialization of the same tree with a fragile
format and no way to query it in place. The reasoning is in
[`clang-usage.md`](clang-usage.md) §2.

**What is deliberately not indexed**, each a default in `cg::Options` and each
because including it costs more than it explains: system headers, function-local
variables, parameters, implicit instantiations, and compiler-generated
declarations. The one that shows up in the numbers is the first — it is why
about half of all call edges land on a stub, which §9 below reports rather than
hides. The full table is [`clang-usage.md`](clang-usage.md) §4.

---

## 4. Semantic model

Nodes are identified by **USR**, not by name. Around that identity the model
records what the AST actually supports and omits what it does not.

**Nodes:** `namespace`, `namespace_alias`, `class`, `struct`, `union`, `enum`,
`enumerator`, `function`, `method`, `constructor`, `destructor`,
`conversion_function`, `field`, `variable`, `typedef`, `template_parameter`,
`macro`. There is no `lambda` kind, because kinds are derived from the AST node
type and a lambda is a closure class with its own call operator — so it appears
as a `class` and a `method`. It is still its own symbol, and the visitor
attributes a lambda body's calls to the closure rather than to the function
that created it, which is the distinction the corpus asserts.

**Edges**, and what each claims:

| Edge | Claim |
| --- | --- |
| `calls` | a resolved call: this declaration is the one the compiler chose |
| `calls_indirect` | through a function pointer; names the variable, not a guess at its value |
| `references` | a use that is not a call — an address taken, a type named |
| `inherits` | a base class relationship, with access and virtuality |
| `overrides` | this method overrides that one |
| `contains` | lexical containment: namespace→class→method |
| `instantiates` / `specializes` | template instantiation and specialization |
| `field_type`, `var_type`, `param_type`, `returns` | type relationships |
| `aliases` | a typedef or alias and the type it names |
| `includes` | file→file, from the preprocessor (its own table, not an edge) |

**Stubs.** A declaration the index resolves but does not analyze — almost always
in a system header — is recorded as a stub: a name, an identity and a location
with no body. This is what makes `callers(std::vector<int>::resize)` answerable
while keeping libstdc++'s twenty overloads out of every answer.

**What the model does not claim** is as much a part of it as what it does: a
call through a function pointer is never recorded as resolved, a dependency
that holds only under run-time dispatch is reported as *possible* rather than
*direct*, and a macro call is not a call because macros leave no trace in the
AST after preprocessing. → [`semantic-model.md`](semantic-model.md), especially
§9.

---

## 5. Storage

SQLite, in two layers, plus a window function.

**The raw layer** holds what each translation unit said, keyed by `tu_id`.
Nothing is merged or deduplicated: a header included by fifty translation units
appears fifty times, because fifty compilations each saw it. **The merged
layer** is one row per USR, rebuilt from the raw layer by `rebuild_symbols()`,
where a header's fifty reports collapse to one answer. The winner is chosen by
`ROW_NUMBER()` preferring, in order: a full record over a stub; a report that
knows the definition over one that does not; a project file over a system
header; and finally deterministically by file and line, so that adding an
unrelated file cannot change an existing answer.

Because the merged layer is derived, it can always be thrown away and rebuilt —
which is what makes the merge rules safe to change. **The raw layer is the
incremental boundary**: re-indexing a file deletes exactly its rows and inserts
replacements, and the merge is recomputed over the remainder.

Two details are load-bearing and were both found by measuring rather than
reasoning. Path→id and id→path caches took `find_symbol` from 108 ms to 1.3 ms.
`ANALYZE` is not optional: without planner statistics SQLite chose to scan every
edge in the database rather than seek the three that matched, and the same query
went from 53 ms to 0.02 ms.

→ [`architecture.md`](architecture.md) §3.4.

---

## 6. MCP

The server speaks JSON-RPC over stdio and exposes **19 tools**: `find_symbol`,
`get_symbol`, `get_function`, `get_class`, `search_symbols`, `get_callers`,
`get_callees`, `get_references`, `get_inheritance`, `get_symbol_dependencies`,
`get_source_context`, `get_impact_analysis`, `get_changed_symbols`,
`get_file`, `get_file_symbols`, `get_includes`, `get_file_dependencies`,
`get_diagnostics`, `get_index_status`.

The design rule is that an answer names symbols and locations, and source is
fetched only when asked for. A question about who calls a function returns the
caller, its file and its line — roughly half a kilobyte — rather than the files
containing them. An agent that wants the body then calls `get_source_context`,
which returns the region around the symbol rather than the file.

Three properties are worth calling out because they are correctness properties
rather than features:

* **Overloads are never guessed at.** `find_symbol("foo")` where three
  declarations share the name returns three candidates with their signatures,
  not one arbitrary answer. Every tool that takes a symbol also accepts a
  `file:line`, which is the unambiguous spelling, and answers hand that back so
  a follow-up question needs no disambiguation.
* **A cut list says it was cut.** Any tool that pages reports its true total, so
  a truncated answer cannot be mistaken for a complete one. This was audited
  across every paged tool rather than fixed where it was noticed.
* **Accuracy is reported, not assumed.** `get_index_status` says whether the
  index was built with a real compilation database (`exact`), a fallback
  (`degraded`) or neither (`unknown`), and whether the tree has moved since.

→ [`mcp.md`](mcp.md) for every tool, its arguments and worked examples.

---

## 7. Incremental indexing

A change is detected by comparing each translation unit's recorded mtime and
size against the filesystem, and re-analyzing only those that differ. A source
file that changed re-indexes itself; a header that changed re-indexes every
translation unit that includes it, which is the correct and more expensive case.

Measured on googletest: a full index is 120.08 s, and re-indexing after one
source file changed is **9.63 s** for 1 translation unit re-analyzed and 107 left
alone — 12.5×. That 9.63 s is the cost of a single unity build and is this
project's worst case rather than its typical one.

The part that scales with the *project* rather than with the change is the
merge: 3.28 s over 55 231 symbols. That is the number to watch as a repository
grows, and it is the one an incremental design has to keep bounded.

Git integration sits on top: `get_changed_symbols` takes a revision or range,
attributes each changed line range to the innermost symbol containing it, keeps
the enclosing chain alongside, and optionally walks the graph outward to report
what could be affected. Lines that fall inside nothing the index knows about are
counted rather than dropped, and a changed file the index has never seen is
named, because an empty answer would otherwise read as "nothing changed".

---

## 8. Tests

**328 tests**, run with `python -m pytest tests/ -q`. The semantic ones drive
the real extractor against a small adversarial corpus and assert on
**resolution**, not on whether syntax nodes exist — which is the only kind of
assertion that distinguishes this tool from a parser.

| Construct | What is asserted |
| --- | --- |
| Overloads | an `int` argument lands on the `int` overload, a `double` on the other, a two-argument call on its own; three calls reach three different symbols |
| Virtual dispatch | a call through a base pointer reaches the pure virtual; through a concrete object, the override; an override is *not* reported as a caller of its base |
| Inheritance | single, multiple (both bases kept), transitive descendants, virtual bases flagged |
| Templates | the primary template is indexed; an instantiation is recorded separately, points back at the template, and is what a call resolves to; two instantiations are two symbols |
| Lambdas | a lambda is its own symbol, and calls in its body belong to it rather than to the function that created it |
| Function pointers | recorded as indirect, never as a resolved call; both the pointer and the address-taken function are recorded |
| Namespaces & nesting | a namespace is part of the qualified name; a nested class is qualified by its enclosing class; an unqualified name is ambiguous across namespaces |
| C | struct tags distinct from typedefs, enums, globals, function pointers with types, `static` internal linkage, macros recorded by definition site with no call claimed through them |
| Types | return types, reference parameters not changing the signature, field and variable types |
| Cross-TU | a definition in another file is found; a function is one symbol across translation units |
| Source context | the innermost container of a line is its function; the region is small; a symbol longer than the cap is truncated *and says so* |
| Truncation | every paged tool reports its true total; an uncut list says it is uncut |

The corpus is deliberately small and adversarial rather than large and
representative, which is a real limitation: it proves the hard cases resolve,
not that every construct in a large codebase does. The googletest evaluation is
what covers breadth, and it measures resolution rates rather than asserting
them.

---

## 9. Performance

Measured on googletest (108 translation units, C++17) and on this tool's own
extractor, on a 4-core/8-thread laptop with a desktop session running. Full
method and caveats: [`evaluation.md`](evaluation.md) §1.

**Indexing.** 120.08 s for 108 translation units, 1.11 s each, 0 failed.
`cg-index` adds about **1.4%** over `clang++ -fsyntax-only` on the same file
(1864 MB vs 1840 MB, 34.85 s vs 35.87 s) — the memory and time are the C++ front
end's cost, not this tool's.

**Database.** 575.6 MB, 55 231 symbols (36 102 in project files), 656 518 edges,
121 674 include edges. 10.9 KB per symbol, dominated by the raw layer, which is
deliberate — it is what makes dropping a translation unit a `DELETE` rather than
a reconstruction. The merged layer rebuilds in 3.28 s.

**Query latency**, over 200 symbols drawn with a fixed seed:

| Tool | median | p95 | median payload |
| --- | ---: | ---: | ---: |
| `get_symbol` | 0.48 ms | 5.08 ms | 492 B |
| `get_callers` | 0.28 ms | 1.30 ms | 539 B |
| `get_callees` | 0.34 ms | 6.93 ms | 433 B |
| `get_impact_analysis` | 0.44 ms | 1.51 ms | 872 B |
| `get_source_context` | 0.52 ms | 1.05 ms | 2199 B |

The median answer costs half a millisecond and half a kilobyte. The worst case
is a hub — `get_symbol` on a symbol with 3606 callers costs 143 ms, because an
exact `COUNT(DISTINCT src)` is O(rows) and the exactness is the point. It is
confined to the handful of symbols in any project that are hubs.

**Semantic resolution.** Of all 656 518 edges, 76.7% land on a symbol defined in
a project file, 19.0% on a stub, 4.2% on a synthetic TU-local identity. For
**call** edges alone — the ones an agent follows to understand behaviour —
**45.8% resolve to a body the index holds**. That is the honest headline and it
is lower than the others because calls are what leave the project.

One caveat on these: they were measured before the type-edge fix, which
restored `field_type`, `returns` and the rest, so the denominator is smaller
than a current run would produce. The *call* percentage — the number that
matters for an agent following behaviour — is unaffected, because calls were
never the edges that were missing. [`evaluation.md`](evaluation.md) §3 marks
which figures moved and which did not.

**Incremental.** 12.5× on a one-file change (§7).

**Bottlenecks found and deliberately not fixed:** `get_index_status` at 19.3 ms
(scans every edge for a count), `search_symbols` at 16.5 ms (`LIKE '%text%'`
cannot use an index), hub `get_symbol` at 143 ms, and the 1.8 GB single-file
peak. Each was measured, diagnosed, and left alone with the rejected fix
recorded in [`evaluation.md`](evaluation.md) §8.

**Not measured:** the worker-count scaling curve. The sweep is written and works
([`scripts/scaling.py`](../scripts/scaling.py)) but has not been run to
completion on a machine that can fit the parallel widths; `evaluation.md` §4
says what is known and what is owed rather than estimating it.

---

## 10. Limitations

Stated plainly, in rough order of how likely they are to matter. This is the
summary; [`limitations.md`](limitations.md) is the full list, organised by what
a reader would be doing when they hit each one.

**Resolution is bounded by what is indexed.** 45.8% of call edges resolve to a
body the index holds. The rest are calls into libstdc++ and other system
headers, which are deliberately not indexed. `get_callers` of a stub is
answerable; `get_callees` of one is empty because there is nothing to report.

**Correct configuration is assumed.** Without a `compile_commands.json`, include
paths, the language standard and feature macros are guesses. The failure is
silent in the worst way: a wrong standard loses everything behind the first
`if constexpr` and shows up as a *missing symbol* rather than an error. The tool
reports `exact`/`degraded`/`unknown` and will not claim completeness, but it
cannot detect every way a configuration is wrong.

**No whole-program analysis.** Each translation unit is analyzed independently
and the results merged. There is no points-to analysis, no aliasing, no
dataflow. A call through a function pointer is recorded as indirect rather than
resolved, because resolving it would be a guess.

**Virtual dispatch is static.** A virtual call records the declaration the
compiler resolved to statically; the runtime targets appear in impact analysis
as *possible*, never as fact.

**Templates are partially covered.** The primary template is indexed and the
instantiation used at a call site is recorded, but walking implicit
instantiations is off by default because it is expensive on template-heavy code.
Template metaprogramming is largely invisible.

**Macros are recorded, not expanded.** Definition sites are indexed; a call
through a macro is not claimed as a call, because macros leave no trace in the
AST after preprocessing.

**Incremental granularity is the file.** There is no partial re-analysis of a
changed function. A header change re-indexes every translation unit that
includes it — correct, and potentially expensive on a widely-included header.

**Memory is unbounded per worker.** Worker count comes from `os.cpu_count()`
alone, so on a machine with fewer free gigabytes than cores the parallel indexer
will swap rather than slow down gracefully. This is measured
([`evaluation.md`](evaluation.md) §4) and is a real limitation, not a
theoretical one.

**The test corpus is small.** Six files, chosen adversarially. It proves the
hard constructs resolve; it does not prove coverage of a large codebase.

**Not every construct in the AST is modelled.** Parameters and locals are off by
default, implicit declarations are skipped, and unnamed TU-local entities get
synthetic identities (4.2% of edges) rather than a stable cross-TU name.

---

## 11. Git history

25 commits, each a working checkpoint with tests run before it. The sequence is
the development order: scaffold and reference analysis, then the extractor, then
the store, then the query layer, then the agent surface, then measurement and
the fixes measurement forced.

```text
011fc3a  chore: project scaffolding and reference analysis
4451eef  feat(cpp): add clang semantic extractor emitting JSONL facts
30b93d9  feat(cpp): mark stub symbols in the fact stream
8ea50de  fix(cpp): attribute a lambda body's calls to the lambda, not its creator
e122fa7  feat(store): add the fact-stream reader and the sqlite semantic store
2647ba1  feat(index): discover compilation databases and index incrementally
eb364da  feat(query): add the semantic query layer
570ffd6  fix(cpp): name symbols, types and linkage the way a reader means them
f176467  fix(query): fold relationships reported by every translation unit
6beb803  test(cpp): add a corpus asserting semantic resolution
9881746  fix(index): put a symbol where it actually is
d126cd6  feat(query): return a small source region around a symbol
998c108  fix(store): keep the definition when merging a header's reports
c46e130  feat(git): say which symbols a diff changed
11b247a  fix(query): count calls, not translation units
524b5f6  feat(tools): expose the index as questions an agent can ask
5484826  feat(mcp): serve the tools to a coding agent over stdio
cb49585  feat(cli): build the index and ask it questions from a shell
b725a8d  perf(query): measure a real project, and fix what it showed
496929f  build(cpp): drop a Clang library nothing links against
0e3aa30  feat(scripts): generate a compilation database for a tree without one
8231606  fix(tools): say when a list was cut, and how long it really was
25452e3  perf(eval): measure a sample of symbols, and report resolution honestly
c014bff  docs: describe the architecture, the model, the interface and the cost
dc829f2  fix(tools): finish the truncation audit the first pass started
```

Two features of the history are worth noting because they were deliberate.

**The `fix` commits are the interesting ones.** `fix(cpp): attribute a lambda
body's calls to the lambda, not its creator`, `fix(query): count calls, not
translation units` and `fix(store): keep the definition when merging a header's
reports` are each a case where the first implementation was plausible and wrong,
and where the corpus caught it. They were committed separately from the features
they correct rather than squashed into them, so the defect and its correction are
both legible.

**Measurement is its own checkpoint.** `b725a8d` and `25452e3` exist because
running the tool on a real project found things reasoning had not: a query that
took 53 ms because SQLite lacked planner statistics, and a "resolution rate"
metric that read 100% because it was counting stubs as resolutions. Both are
recorded, including the retracted scaling measurement in
[`evaluation.md`](evaluation.md) §4, on the principle that a wrong number that
was published and then corrected is more useful to a reader than one that
quietly disappeared.

---

## Where to go next

* **New to the project** — [`../README.md`](../README.md), then
  [`mcp.md`](mcp.md) §3 for how an agent is meant to use it.
* **Evaluating the design** — [`architecture.md`](architecture.md) §4 records
  the alternatives each decision beat, including the ones that were rejected.
* **Extending it** — [`clang-usage.md`](clang-usage.md) §3 is the pipeline
  inside the extractor; the analyzer is the replaceable part, and
  [`semantic-model.md`](semantic-model.md) is the interface it must satisfy.
* **Trusting it** — [`evaluation.md`](evaluation.md) §9 says what the numbers do
  not support, which is as important as what they do.
