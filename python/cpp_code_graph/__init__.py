"""A semantic index of a C/C++ codebase, and the questions it answers.

The parts, in the order they run: `discovery` finds the compilation database
and the translation units, `indexer` drives the Clang extractor over them,
`store` keeps the facts in SQLite, `query` answers questions over the merged
symbol table, `changes` and `git` tie the index to a diff, `tools` exposes the
queries as named questions, and `mcp_server` serves those to a coding agent.
"""

__version__ = "0.1.0"
