# Architecture

This document describes what `astroclang` is made of and why. It is the
long-form companion to [`README.md`](../README.md), which is the short version
for someone who just wants to run the tool.

---

## 1. The problem being solved

A syntax-level index of a C++ repository answers questions about text. That is
not enough, because in C++ the text does not determine the meaning.

```cpp
obj.resize(100);
```

A parser that reads tokens can say "a call to `resize`". What a reader needs to
know is which declaration that is — `std::vector<int>::resize(size_t)`,
`MyBuffer::resize(size_t)`, or a template instantiation of either. The
difference is decided by overload resolution, by the static type of `obj`, by
which namespaces are in scope, and by template argument deduction. None of
those facts are in the source text at the call site. They exist only after
semantic analysis, inside a compiler, with the same compiler arguments the
build uses.

So the design follows from one decision: **the graph is built from the
compiler's own view of the program, not from a parse of its text.** Everything
else — the fact stream, the two-layer store, the `file:line` handles — exists
to make that decision affordable.

---

## 2. The pipeline

```
                C/C++ repository
                       |
                       v
              Compilation database          discovery.py
                       |
                       v
                Clang analysis              cpp/ (astroclang-index, LibTooling)
                       |
                       v
                 Fact stream                facts.py
                       |
                       v
                Semantic store              store.py, schema.py
                       |
              +--------+--------+
              |                 |
              v                 v
        Query layer          Git diff        query.py    git.py, changes.py
              |                 |
              +--------+--------+
                       |
                       v
                  MCP server                tools.py, mcp_server.py
                       |
                       v
                 Coding agents

                       ^
                       |
                  Command line                  cli.py
```

Each stage is a separate module with one job, and the seams are the ones the
specification named. The two front ends (MCP and the CLI) sit on the same
`tools.call` dispatch, so they cannot drift into answering one question two
ways.

---

## 3. Components

### 3.1 `astroclang-index` — the extractor (`cpp/`)

A C++ binary built on Clang's LibTooling. It is given **one translation unit**
and the compiler arguments for it, and it writes one **fact stream** to stdout.

It is a separate process for three reasons:

* **Isolation.** A Clang front end can crash on adversarial input. A crash
  costs one translation unit's facts, not the index.
* **Parallelism.** Translation units are independent by construction; separate
  processes are how that independence is used. The measured speedup saturates
  at about 2.15× on this machine's four physical cores, and eight workers are
  not faster than four (see [`evaluation.md`](evaluation.md) §4).
* **Diffability.** The output is JSON Lines on stdout, so a test can assert on
  exactly what the analyzer saw, and a human can `grep` it.

The binary has no opinion about storage. It never opens a database, never reads
a file it was not told about, and holds no global state between translation
units. That keeps it a pure analyzer and keeps the interesting policy — what to
merge, what to keep, what to discard — in one place on the other side.

Its analysis is described in [`clang-usage.md`](clang-usage.md).

### 3.2 `discovery.py` — what to analyze, and how confidently

Two jobs:

* **Find a compilation database.** The search is thorough (root, then the
  conventional build directories, then a recursive walk, newest conventions
  first) because without one the analysis is materially worse. `compile_commands.json`
  supplies include paths, the language standard, `-D` macros and the target
  triple; a header that is not on the include path is a header whose
  declarations are simply absent.
* **Plan the fallback when there is none.** A source file with no database is
  still indexed, with include directories guessed from convention, and the plan
  records `degraded = True`. The degradation is carried through to the answer:
  `get_index_status` reports the accuracy as `exact`, `degraded` or `unknown`,
  and the CLI prints a warning about it even under `--quiet`. **Reduced accuracy
  is reported, never papered over.**

`SOURCE_EXTENSIONS` deliberately excludes `.h`. A header is indexed through the
translation units that include it; handing one to Clang as a source file
invents a translation unit that does not exist in the build, with different
macro state and a different meaning.

### 3.3 `facts.py` — the wire format, and its one dangerous property

A streaming reader for the extractor's output. Each record is a flat JSON
object with a one-letter type discriminator:

| Record | Meaning |
| --- | --- |
| `f` | a file, interned per translation unit |
| `sym` | a declaration or definition |
| `edge` | a relationship between two USRs |
| `inc` | an `#include` |
| `diag` | a compiler diagnostic |
| `meta` | key/value metadata about the run |
| `done` | end of stream |

The property that has to be understood to use this module: **file ids are
interned per translation unit.** They are small integers starting from zero on
every run, and the same header gets a different number in the next run. A
consumer that treated them as global would attribute every edge to whichever
file happened to share the number. `store.ingest` remaps every id through the
global file table for exactly this reason, and it is why `TranslationUnit`
carries a `files` list rather than having the edges carry paths.

### 3.4 `store.py` + `schema.py` — the persistent index

SQLite. The design is two layers and a `ROW_NUMBER()` window function.

**The raw layer** (`raw_symbol`, `raw_edge`, `raw_include`, `raw_diag`, `tu`)
holds what each translation unit said, keyed by `tu_id`. Nothing is merged here
and nothing is deduplicated across units. A header included by fifty
translation units appears fifty times, which is the truth: fifty compilations
each saw it.

**The merged layer** (`symbol`) is rebuilt from the raw layer by
`rebuild_symbols()`, one row per USR. A header's fifty reports have to collapse
to one answer, and the interesting part of the system is *how*:

The winner is picked by `ROW_NUMBER()` in this order:

| Prefer | Because |
| --- | --- |
| a full record over a stub | a stub is a name and a location; a full record has a signature |
| a report that knows the definition over one that does not | only the TU holding the body can say where the body is, and picking by file and line alone would make the answer depend on how many files happened to be indexed |
| a project file over a system header | otherwise every answer sends the reader into libstdc++ |
| deterministically, by file and line | so that adding an unrelated file cannot change an existing answer |

The merged layer is derived, so it can always be thrown away and rebuilt. That
is what makes the merge rules safe to change: they are not data, they are code
that runs over data.

**The raw layer is the incremental boundary.** Re-indexing one file deletes
exactly its rows (`DELETE FROM tu WHERE id = ?`, cascading) and inserts
replacements. Nothing else moves, and the merge is recomputed over the
remainder.

`Store` also owns three caches — path→id, id→path, id→in-project — because a
query builds one entry per symbol it reports, and a project with sixty thousand
symbols otherwise spends a few hundred thousand statements reading a table that
fits in a few kilobytes. Measured on the googletest index, routing those two
lookups straight at SQL instead of through the caches takes `find_symbol` from
**0.55 ms to 1.23 ms** — a factor of 2.3 on a query that is on the path of
nearly every question. It is worth having, and it is worth being accurate about:
the saving is seven tenths of a millisecond, not the order of magnitude an
earlier draft of this section claimed. The same measurement shows the cache
doing *nothing* for `search_symbols` (21.5 ms either way) or for `get_callers`
on a hub (54.6 ms against 57.7 ms), because both are dominated by something
else — a `LIKE` scan over every symbol, and counting a symbol's callers
respectively.

`Store.analyze()` runs `ANALYZE`. It is cheap insurance rather than a fix for a
measured pathology: with the composite indexes in place (`idx_raw_edge_dst` is
on `(kind, dst)`, so a `dst`-only lookup is served by SQLite's skip-scan),
re-measuring the queries this section used to blame on missing statistics shows
no difference between having them and not — 1.73 ms against 1.73 ms for a
counted edge lookup, 29.6 ms against 28.9 ms for a hub's distinct-caller count.
`REBUILD_SYMBOLS` runs a six-way join over the whole raw layer, which is the
kind of plan statistics genuinely inform, so it is gathered; but the earlier
claim here that statistics took a query "from 53 ms to 0.02 ms" is not
reproducible against the current schema and has been removed rather than left
standing. An index built by an older version has no statistics, so
`index_project` gathers them whenever it indexed something *or* the index has
none — re-indexing is already how a user repairs a stale index, and it should
not need a second flag.

### 3.5 `query.py` — the graph

Questions over the merged layer: symbol lookup and resolution, callers and
callees, references, inheritance and overrides, includes and file
dependencies, containment, source regions, impact traversal.

Two rules run through it, and they are the reason it is a separate module from
`tools.py`:

* **Never claim more than the index knows.** A call through a function pointer
  is `calls_indirect`, not `calls`. A dependency that holds only under run-time
  dispatch is labelled `possible` and says why. Overloads that a name could
  mean come back as a list of candidates rather than as a guess.
* **Counts are exact; lists are bounded.** `degree()` counts distinct
  relationships with one indexed query — exact, and cheap — while `callers()`
  takes a limit. A list that has been cut says so and carries its true length.

### 3.6 `tools.py` — the questions, declared as data

Nineteen tools, each a name, a title, a description written for an agent, a JSON
Schema, and a handler. Declaring them as data rather than as nineteen functions
means the MCP server, the CLI and the tests all read the same list, and the
answer to "what can this thing tell me" cannot drift from what it does.

The four rules in its header are the specification for every answer shape:

* **Nothing returns a source file.** `get_source_context` is the only tool that
  returns text, and it returns a padded region with line numbers.
* **Every symbol is named `file:line`,** and that spelling resolves exactly when
  passed back. An agent never has to handle a USR, and one answer is directly
  usable as the next question.
* **A truncated list says so,** and carries its true length.
* **Nothing is guessed.**

### 3.7 `git.py` and `changes.py` — a diff, as symbols

`git.py` knows about git and nothing about the index: it turns a revision or a
working tree into "these lines of this file are new", which is a question git
answers exactly.

`changes.py` attributes each changed range to the **innermost symbol that
contains it**, keeping the enclosing chain alongside — a change inside a method
is also a change to its class. Lines that fall inside nothing the index knows
about are counted rather than dropped, because a diff over an unseen file would
otherwise appear to have changed nothing at all.

### 3.8 `mcp_server.py` — the protocol

MCP is JSON-RPC 2.0 with a small vocabulary, and the stdio transport is one
JSON message per line. That is the whole of it, which is why this module speaks
it directly rather than adopting a framework: **the protocol is smaller than the
dependency**, and the failure modes that matter here are easier to see in code
that has nowhere else to put them.

The two failure modes are both about stdout:

* stdout belongs to the protocol. A stray `print`, a log line, a warning from a
  library, and the stream is corrupt — so all diagnostics go to stderr.
* a notification has no `id` and gets no reply. Answering one is not a
  politeness the client will tolerate.

### 3.9 `cli.py` — the same answers from a shell

`astroclang index .` then `astroclang mcp`. The query subcommands are a
table that maps a command name to a tool name and a renderer, and they run
through the same `tools.call`. What the CLI adds is argument parsing, a human
rendering of the common answers, and exit statuses a script can branch on:

| Status | Meaning |
| --- | --- |
| `0` | the question was answered (an empty answer is still an answer) |
| `1` | the question could not be asked — no index, an ambiguous name, a bad revision. The error is a JSON payload on stderr |
| `2` | usage was wrong |

---

## 4. Decisions, and the alternatives they beat

**Clang LibTooling in a separate binary, not clangd.**
clangd is a language server: it owns a background indexer, a file-watching
lifecycle, and an index format (`IndexData`/`Symbol`/`Ref`) designed for
*interactive* symbol lookup. Its index is keyed by symbol name and stores
references as file/line pairs; it does not carry the containment hierarchy, the
declaration/definition split, or the edge kinds this graph needs, and its
lifetime model assumes a live LSP client. Using it would have meant adopting an
architecture built for a different question and then fighting it. LibTooling
gives the AST directly, which is where the semantic facts actually are. The
full comparison is in [`reference-analysis.md`](reference-analysis.md) and the
mechanics in [`clang-usage.md`](clang-usage.md).

**SQLite, not a graph database.**
The queries are almost all "one node, then its neighbours" or "one indexed
COUNT". A property-graph database buys traversal expressiveness that this
workload does not use, at the cost of a dependency, a server, and a query
language to learn. SQLite gives ACID, WAL for concurrent reads during a
re-index, and a query planner whose statistics turned out to matter more than
any schema choice. It also means the index is one file a user can copy, delete,
or inspect with a tool they already have.

**A fact stream between the analyzer and the store, not an in-process API.**
An in-process design would be faster and would couple the C++ analysis to the
Python storage. The stream costs one serialization pass and buys: the extractor
stays a pure function of one translation unit, the store can change its schema
without rebuilding Clang code, and the analysis output is directly assertable
in tests.

**Two layers, not one.**
The raw layer is what makes incremental indexing correct. Merging on write
would mean that undoing a re-index requires reconstructing what the previous
merge looked like; with a raw layer, dropping a translation unit is a `DELETE`
and the merge is a pure function of what remains.

**`file:line` as the inter-query handle, not a USR.**
A USR is the right identity for the graph and the wrong thing to make an agent
copy between calls. `file:line` is what a human would say, it is short, and it
round-trips: every symbol-shaped argument accepts it and every symbol-shaped
result emits it, so one answer is directly usable as the next question. The
three tools that describe a single symbol also report its USR — it is the one
string that names that symbol and nothing else, which is what a reader wants
when citing a finding — but no tool ever *requires* one back.

---

## 5. What is deliberately absent

* **No other languages.** The architecture has seams where another front end
  could plug in — the fact stream is language-independent, and the store does
  not know what a class is — but nothing was added for the sake of
  completeness. C and C++ quality is the goal.
* **No AI review logic.** `get_changed_symbols` reports what a diff touched and
  what that could affect, in three degrees of certainty. It does not decide
  whether the change is good. The semantic index has to be reliable before
  anything is layered on top of it.
* **No build system integration.** CMake, Bazel and Meson all already emit
  `compile_commands.json`, which is the interface this tool takes. Reaching
  into a build system to generate one would be re-implementing a solved problem
  and a much larger surface.
