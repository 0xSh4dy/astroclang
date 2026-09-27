# cpp-code-graph

A semantic index of a C/C++ codebase, built from Clang, and served to coding
agents over MCP.

The point is that a syntax-oriented index cannot answer the questions that
matter in C++. `obj.resize(100)` is not a call to `resize`; it is a call to one
specific declaration out of however many that name denotes, chosen by overload
resolution, template instantiation and the arguments in scope. This tool gets
that answer from the compiler itself - the same front end that would build the
code - and resolves every reference to the declaration it actually means, with
the type information that only exists after semantic analysis.

## Install

The extractor is a separate binary; build it first.

```sh
cmake -B build -G Ninja && cmake --build build      # produces build/cpp/cg-index
pip install -e python/                              # the `cpp-code-graph` command
export CG_INDEX=$PWD/build/cpp/cg-index             # or put cg-index on PATH
```

There are no Python dependencies. The index is SQLite and the MCP server speaks
JSON-RPC directly.

## Use

```sh
cpp-code-graph index .                  # reads compile_commands.json if there is one
cpp-code-graph callers Foo::resize      # who calls it
cpp-code-graph impact Foo::resize       # what a change could reach, by degree
cpp-code-graph context Foo::resize      # a small region of source around it
cpp-code-graph changed HEAD~1           # which symbols that revision touched
cpp-code-graph mcp                      # serve the same answers over MCP
```

Every answer names its symbols as `file:line`, and every command that takes a
symbol accepts that spelling back, so an answer can be handed to the next
question without copying anything. `--json` prints the payload an agent would
receive. Run `cpp-code-graph <command> --help` for the rest.

## What it knows

Symbols are identified by Clang's USR, so `foo(int)` and `foo(float)`, `A::foo`
and `B::foo`, and a template and its instantiations are distinct entities rather
than one name. Around them the index records declarations, definitions, calls
(resolved), references, inheritance, overrides, field and variable types,
includes, namespaces and containment.

Two things are worth knowing before trusting an answer:

* Without a `compile_commands.json`, the tool falls back to conventional
  include directories. Include paths, language standard and feature macros are
  then guesses, so headers may be missed and declarations behind them absent.
  `cpp-code-graph status` reports the accuracy as `exact`, `degraded` or
  `unknown` rather than leaving it to be assumed.
* The index describes the tree it was built from. If the revision has moved
  since, `status` says so.

## Layout

| Module | What it owns |
| ------ | ------------ |
| `discovery` | compilation databases, translation units, fallback plan |
| `indexer` | running the extractor, incrementally |
| `facts` | the extractor's fact stream |
| `store` | SQLite: raw per-unit facts, merged symbol table |
| `query` | questions over the merged graph |
| `changes`, `git` | diffs, and the symbols a diff touches |
| `tools` | the named questions, and the shape of their answers |
| `mcp_server` | those tools over MCP on stdio |
| `cli` | the command line |
