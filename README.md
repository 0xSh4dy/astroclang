# cpp-code-graph

A semantic index of a C/C++ codebase, built from Clang, and served to coding
agents over MCP.

`obj.resize(100)` is not a call to `resize`. It is a call to one specific
declaration out of however many that name denotes, chosen by overload
resolution, template instantiation and the arguments in scope. A syntax-oriented
index cannot say which one, and in C++ that is the only question that matters:
`std::vector<int>::resize`, `MyBuffer::resize` and a `resize` overload three
namespaces away are different functions with different callers, different
impact and different tests.

This tool asks the compiler. It drives the same front end that would build the
code, resolves every reference to the declaration it actually means, and keeps
the answer in a database that an agent can query in half a millisecond instead
of reading the repository again.

```sh
cpp-code-graph index .        # build or update the index
cpp-code-graph mcp            # serve it to a coding agent
```

---

## The two halves

| | What it is | Where |
| --- | --- | --- |
| `cg-index` | a C++ binary built on Clang LibTooling; walks one translation unit and emits a stream of semantic facts | `cpp/` |
| `cpp-code-graph` | the Python index, query layer, git integration and MCP server | `python/` |

The split is deliberate and is argued in [`docs/clang-usage.md`](docs/clang-usage.md) §5.
The short version: Clang's AST is a per-translation-unit, in-memory, C++
structure. The graph is cross-translation-unit, persistent, and merged. Making
one process do both means either holding 108 ASTs in memory or re-parsing to
answer a question. Split, the extractor can be a short-lived process that dies
after each file and leaves facts behind, and the index can answer questions
about a project nobody currently has loaded.

## Install

The extractor is a separate binary and needs LLVM/Clang 17 development
libraries; build it first.

```sh
cmake -B build -G Ninja && cmake --build build   # produces build/cpp/cg-index
pip install -e python/                           # the `cpp-code-graph` command
export CG_INDEX=$PWD/build/cpp/cg-index          # or put cg-index on PATH
```

There are no Python dependencies. The index is SQLite (via the standard
library) and the MCP server speaks JSON-RPC over stdio directly.

## Use

```sh
cpp-code-graph index .                  # reads compile_commands.json if there is one
cpp-code-graph status                   # what is indexed, and how far to trust it
cpp-code-graph callers Foo::resize      # who calls it
cpp-code-graph callees Foo::resize      # what it calls
cpp-code-graph impact Foo::resize       # what a change could reach, by degree
cpp-code-graph context Foo::resize      # a small region of source around it
cpp-code-graph changed HEAD~1           # which symbols that revision touched
cpp-code-graph mcp                      # serve the same answers over MCP
```

Every answer names its symbols as `file:line`, and every command that takes a
symbol accepts that spelling back, so an answer can be handed to the next
question without copying anything by hand. `--json` prints the exact payload an
agent would receive. `cpp-code-graph <command> --help` lists the rest; the
whole surface is in [`docs/mcp.md`](docs/mcp.md).

### Pointing an agent at it

The MCP server speaks stdio, so it is configured like any other:

```json
{
  "mcpServers": {
    "cpp-code-graph": {
      "command": "cpp-code-graph",
      "args": ["mcp", "/path/to/your/project"]
    }
  }
}
```

An agent that would otherwise read a dozen headers to find out who calls
something gets a 500-byte answer naming the caller, its file and its line, and
pulls source only for the one region it decides to read. That progression —
repository, file, symbol, relationships, and only then source — is the point of
the whole tool, and [`docs/mcp.md`](docs/mcp.md) §3 is where it is specified.

## What it knows

Symbols are identified by Clang's USR, so `foo(int)` and `foo(float)`, `A::foo`
and `B::foo`, a template and each of its instantiations, and a `static` function
that shares a name with one in another file are all distinct entities rather
than one name. Around them the index records declarations, definitions, calls
(resolved), references, inheritance, overrides, field and variable types,
includes, namespaces and containment.

Node kinds, edge kinds and exactly what each edge claims are documented in
[`docs/semantic-model.md`](docs/semantic-model.md).

## What to be careful about

Two things are worth knowing before trusting an answer.

**Without a `compile_commands.json`, include paths, language standard and
feature macros are guesses.** Declarations behind a missed include are then
absent from the index, which shows up as a missing symbol rather than as an
error. The tool falls back to conventional include directories rather than
refusing, but it reports the accuracy as `exact`, `degraded` or `unknown` and
will not pretend the analysis was complete. `scripts/make_compdb.py` generates a
database for a tree that has none.

**The index describes the tree it was built from.** If the revision has moved
since, `status` says so, and a diff is taken against the revision the index
remembers.

The constructs the index does not see at all are listed in
[`docs/limitations.md`](docs/limitations.md); the measured resolution rate and
the bottlenecks that were found and left alone are in
[`docs/evaluation.md`](docs/evaluation.md). None of it is buried here on the
grounds that a short README reads better.

## Documentation

| Document | What it answers |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | what each component is for, and the decisions behind it |
| [`docs/clang-usage.md`](docs/clang-usage.md) | which Clang APIs, why LibTooling over clangd/ASTMatchers/libclang, and what is deliberately not indexed |
| [`docs/semantic-model.md`](docs/semantic-model.md) | identity, node kinds, edge kinds, containment, and what the model does not claim |
| [`docs/limitations.md`](docs/limitations.md) | what the index does not see, does not resolve, or resolves differently than a reader would expect |
| [`docs/mcp.md`](docs/mcp.md) | every MCP tool, its arguments, and worked examples |
| [`docs/evaluation.md`](docs/evaluation.md) | indexing cost, database size, query latency, resolution rate, incremental speedup |
| [`docs/reference-analysis.md`](docs/reference-analysis.md) | what was learned from `code-review-graph`, and what was rejected |
| [`docs/final-report.md`](docs/final-report.md) | the project as a whole, and the commit history |

## Layout

```
cpp/               the Clang extractor (cg-index)
  include/cg/      AnalysisVisitor, Indexer, FactWriter, the compilation-database factory
  src/             their implementations, and main
python/
  cpp_code_graph/  the index, queries, git integration, MCP server, CLI
  tests/           the test suite, and the C/C++ corpus it asserts against
scripts/           evaluation, compilation-database generation
docs/              the documents above
```

Within the Python package:

| Module | What it owns |
| --- | --- |
| `discovery` | compilation databases, translation units, fallback plan |
| `indexer` | running the extractor, incrementally |
| `facts` | the extractor's fact stream |
| `store` | SQLite: raw per-unit facts, merged symbol table |
| `query` | questions over the merged graph |
| `changes`, `git` | diffs, and the symbols a diff touches |
| `tools` | the named questions, and the shape of their answers |
| `mcp_server` | those tools over MCP on stdio |
| `cli` | the command line |

## Tests

```sh
cd python && python -m pytest tests/ -q
```

333 tests. The semantic ones run the real extractor against a small adversarial
corpus and assert on resolution, not on syntax: that an `int` argument lands on
the `int` overload and a `double` argument on the other, that a call through a
base pointer reaches the pure virtual while a call through a concrete object
reaches the override, that a lambda's body belongs to the lambda rather than to
the function that created it, and that two instantiations of one template are
two symbols. `CG_INDEX` points the suite at the extractor binary.

## License

MIT. See [`LICENSE`](LICENSE).
