#!/usr/bin/env python3
"""Write a compilation database for a tree that does not ship one.

`scripts/evaluate.py` measures a project through its own `compile_commands.json`,
which is the configuration the tool is meant to be given.  Some real trees do not
have one - a source package under `/usr/src`, a project whose build happens
somewhere else - and indexing those without it would measure the fallback path
rather than the semantic one.

This writes the closest thing to the project's real configuration that can be
had without building it: every source file, one entry each, with the include
directories the tree's own headers live in and a language standard.  It is a
convenience for measurement, not a substitute for a real database, and the
result still has to be checked against the tree before the numbers mean
anything.

    scripts/make_compdb.py --root /tmp/gt-eval/googletest \\
        --include googletest/include --include googlemock/include \\
        --std c++17 --out compile_commands.json

The standard is the argument that matters most and the one this cannot guess.
A project indexed under a standard older than it needs does not fail loudly:
it fails at the first `if constexpr`, and everything behind that point is
missing from the graph.  googletest at `-std=c++14` reports "C++ versions less
than C++17 are not supported"; at `-std=c++17` it reports nothing.  Check the
generated file against the tree - `cg-index --print-config` prints what a file
would be compiled with - before trusting any number measured through it.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--include", action="append", default=[],
                        help="include directory, relative to the root")
    parser.add_argument("--define", action="append", default=[],
                        help="preprocessor definition, as NAME or NAME=VALUE")
    parser.add_argument("--std", default="c++14")
    parser.add_argument("--compiler", default="clang++")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    flags = [f"-I{root / inc}" for inc in args.include]
    flags += [f"-D{d}" for d in args.define]

    entries = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in SUFFIXES or not path.is_file():
            continue
        std = f"-std={args.std}"
        argv = [args.compiler, std, *flags, "-c", str(path)]
        entries.append({
            "directory": str(root),
            "file": str(path),
            "arguments": argv,
            # A shell-escaped copy, because a reader that only understands
            # `command` would otherwise split `-I/path with a space` in two.
            "command": " ".join(shlex.quote(a) for a in argv),
        })

    args.out.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"{len(entries)} entries -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
