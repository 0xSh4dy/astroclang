# cpp-code-graph

A semantic index of a C/C++ codebase, built from Clang, and served to coding
agents over MCP.

`obj.resize(100)` is not a call to `resize`. It is a call to one specific
declaration out of however many that name denotes, chosen by overload
resolution and template instantiation. This tool gets that answer from the
compiler and resolves every reference to the declaration it means.

## Install

The extractor is a C++ binary built against Clang; build it first.

```sh
cmake -B build -G Ninja && cmake --build build      # or drop -G Ninja
                                                    # produces build/cpp/cg-index
pip install -e python/                              # the `cpp-code-graph` command
export CG_INDEX=$PWD/build/cpp/cg-index             # or put cg-index on PATH
```

Requires LLVM/Clang 16 or newer (development headers and libraries) and Python
3.9 or newer. There are no Python dependencies.

## Use

```sh
cpp-code-graph index .                  # reads compile_commands.json if there is one
cpp-code-graph callers Foo::resize      # who calls it
cpp-code-graph impact Foo::resize       # what a change could reach, by degree
cpp-code-graph context Foo::resize      # a small region of source around it
cpp-code-graph changed HEAD~1           # which symbols that revision touched
cpp-code-graph mcp                      # serve the same answers over MCP
```

Full usage, and what the tool knows and does not, is in
[`python/README.md`](python/README.md).

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/reference-analysis.md`](docs/reference-analysis.md) | the reference implementation this was designed against, and what was adopted or rejected |
| [`docs/architecture.md`](docs/architecture.md) | the five layers, and why each boundary is where it is |
| [`docs/clang-usage.md`](docs/clang-usage.md) | exactly which Clang facilities are used, and the alternatives that were not |
| [`docs/semantic-model.md`](docs/semantic-model.md) | every node and edge type |
| [`docs/mcp.md`](docs/mcp.md) | every MCP tool, its arguments, and real example output |
| [`docs/limitations.md`](docs/limitations.md) | what the index does not see or resolve |
| [`docs/evaluation.md`](docs/evaluation.md) | measured cost and accuracy on real repositories |
| [`docs/final-report.md`](docs/final-report.md) | the whole account, end to end |

## Layout

| Path | What it is |
| --- | --- |
| `cpp/` | `cg-index`, the Clang LibTooling extractor — one translation unit, JSON Lines out |
| `python/cpp_code_graph/` | the store, the query layer, the tools, the MCP server, the CLI |
| `python/tests/` | the test suite, including the C/C++ corpus |
| `scripts/` | `evaluate.py`, `make_compdb.py` |

## Tests

```sh
python -m unittest discover -s python/tests -t python     # all of them
cd python && python -m unittest tests.test_semantics      # just the extractor ones
```

The semantic tests drive the real extractor over the corpus in
`python/tests/corpus/` and assert resolution rather than syntax: not that a
call edge exists, but that it lands on the exact overload.

## License

MIT — see [`LICENSE`](LICENSE).
