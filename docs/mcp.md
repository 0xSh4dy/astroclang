# The MCP interface

`astroclang mcp` serves the index to a coding agent over the Model Context
Protocol on stdio. This document is the reference for what it exposes.

```sh
astroclang index /path/to/project     # build the index
astroclang mcp /path/to/project       # serve it on stdio
astroclang mcp --list-tools           # print the surface as JSON, serve nothing
```

---

## 1. The shape of every answer

Four rules, applied without exception. They exist because the reader pays for
every token and cannot see the repository.

**Nothing returns a source file.** `get_source_context` is the only tool that
returns text, and what it returns is a padded region with line numbers in it.

**Every symbol is named `file:line`, and that spelling resolves exactly when
it is passed back.** An agent can follow one answer to the next question
without ever handling a USR:

```
get_callers("geo::Circle::area")   ->  ... "location": "src/usage.cpp:32"
get_symbol("src/usage.cpp:32")     ->  the caller
```

**A list that has been cut says so, and carries its true length.**

```json
{"callers": [...10 entries...], "caller_count": 3606}
```

The 3606 is real: it is `get_callers` on `testing::internal::CodeLocation::CodeLocation`
in the googletest index, asked with `limit: 10`, and the count is the whole list
rather than the page.

`find_symbol` and `search_symbols` add `more: true` instead, because there they
report a page of a search rather than a bounded list; the count-shaped tools
report the exact total. Either way the reader is never left to guess whether it
has seen everything.

**Nothing is guessed.** A name that denotes three overloads comes back as three
candidates. A dependency that holds only under run-time dispatch is labelled
`possible` and says why.

### Arguments

| Argument | Meaning |
| --- | --- |
| `symbol` | a name (`Foo::resize`), a qualified name with parameters to pick an overload (`Foo::resize(size_t)`), or a `file.cpp:142` location from an earlier result |
| `path` | for a symbol-shaped tool, narrow a name that several files declare; for a file-shaped tool, the file itself. A bare basename is accepted when it is unambiguous |
| `limit` | how many entries to return |
| `include_system` | include symbols from system headers (default false) |
| `kind` | restrict to one node kind (`function`, `class`, …) |
| `transitive` | follow a chain past its first step (default true) |
| `depth` | how many hops to follow |
| `context_lines` | how much source to either side |

Which tool takes which — the schemas are the authority, and `--list-tools`
prints them:

| Tool | Required | Optional |
| --- | --- | --- |
| `find_symbol` | `reference` | `path`, `limit` |
| `get_symbol`, `get_function` | `symbol` | `path` |
| `get_class` | `symbol` | `path`, `limit` |
| `get_callers`, `get_callees`, `get_references` | `symbol` | `path`, `limit`, `include_system` |
| `get_symbol_dependencies` | `symbol` | `path`, `limit` |
| `get_inheritance` | `symbol` | `path`, `limit`, `transitive` |
| `get_source_context` | `symbol` | `path`, `context_lines` |
| `get_impact_analysis` | `symbol` | `path`, `depth`, `limit` |
| `search_symbols` | `query` | `kind`, `limit`, `include_system` |
| `get_file`, `get_file_dependencies` | `path` | `limit` |
| `get_file_symbols` | `path` | `kind`, `limit` |
| `get_includes` | `path` | `transitive`, `limit` |
| `get_changed_symbols` | — | `revision`, `staged`, `include_impact`, `depth`, `limit` |
| `get_diagnostics` | — | `path`, `limit` |
| `get_index_status` | — | — |

A tool that takes no subject is asked a question about the index as a whole.

An **ambiguous name is an error, not a guess**: `get_references("geo::scale")`
where three overloads share the name returns a tool error naming the
candidates, and the agent re-asks with one of their locations.

---

## 2. Finding things

### `find_symbol`

Resolve a name to the symbols it could mean. Use this first when you have a
name but not a location.

```json
{"reference": "scale", "limit": 10}
```
```json
{
 "reference": "scale",
 "match_count": 3,
 "matches": [
  {"symbol": "geo::scale", "signature": "(int)", "kind": "function", "location": "include/shapes.h:77"},
  {"symbol": "geo::scale", "signature": "(double)", "kind": "function", "location": "include/shapes.h:75"},
  {"symbol": "geo::scale", "signature": "(double, double)", "kind": "function", "location": "include/shapes.h:76"}
 ],
 "note": "several symbols match; narrow the question with `path`, or pass one of these `location` values to name exactly one"
}
```

Three overloads, three entries, and the note says what to do about it.

### `search_symbols`

Substring search over every name. Use it when the spelling is unknown, or to
survey a subsystem by prefix.

```json
{"query": "scale", "limit": 5}
```

Accepts `kind` to narrow to one kind, and `include_system`.

### `get_index_status`

Start here when an answer looks empty or incomplete.

```json
{
 "root": "/tmp/ccg-demo",
 "database": "/tmp/ccg-demo/.astroclang/index.db",
 "schema_version": "1",
 "database_bytes": 237568,
 "files": 85, "project_files": 6,
 "translation_units": 4,
 "symbols": 113, "symbols_in_project": 107,
 "edges": 292, "includes": 234,
 "diagnostics": 0, "degraded_tus": 0, "failed_tus": 0,
 "built_at": "2026-09-27T08:58:09+00:00",
 "compiler_arguments": {"compile_commands.json": 4},
 "accuracy": "exact"
}
```

`accuracy` is `exact`, `degraded` or `unknown`. `degraded_tus` counts
translation units analyzed without a compilation database; `failed_tus` counts
those whose compilation reported errors, so declarations behind the error point
are absent from the graph — `get_diagnostics` has the messages. The `note` says
both in words when either is non-zero. A translation unit the extractor could
not produce facts for *at all* is a different thing, and the indexer's own
report counts it as `failed`; it is not surfaced here.

When the index records a git revision and the tree has moved on, this is where
that is reported.

---

## 3. Describing one symbol

### `get_symbol`

Everything the index knows about one symbol.

```json
{"symbol": "geo::Circle::area"}
```
```json
{
 "symbol": "geo::Circle::area",
 "signature": "() const",
 "kind": "method",
 "location": "include/shapes.h:39",
 "defined_at": "src/shapes.cpp:19",
 "usr": "c:@N@geo@S@Circle@F@area#1",
 "type": "double",
 "properties": {"virtual": true, "override": true, "const": true},
 "used_in_translation_units": 2,
 "member_of": "geo::Circle",
 "callers": 3,
 "callees": 0,
 "within": ["geo::Circle", "geo"]
}
```

`callers` and `callees` are **counts, not lists** — exact, and costing one
indexed query each. An agent deciding whether to dig further needs to know
there are forty callers, not to receive forty of them.

### `get_function` and `get_class`

`get_function` is `get_symbol` with callable-specific detail. `get_class`
answers "what implements this interface" and "what is in this class" without
reading the header:

```json
{"symbol": "geo::Tagged"}
```
```json
{
 "symbol": "geo::Tagged",
 "location": "include/shapes.h:58",
 "kind": "class",
 "bases": [
  {"symbol": "geo::Circle", "location": "include/shapes.h:34", "access": "public"},
  {"symbol": "geo::Named",  "location": "include/shapes.h:49", "access": "public"}
 ],
 "derived": [],
 "ancestors": [{"symbol": "geo::Shape", "location": "include/shapes.h:16"}],
 "methods": [
  {"symbol": "geo::Tagged::Tagged", "signature": "(double, int)", "kind": "constructor",
   "location": "include/shapes.h:60", "defined_at": "src/shapes.cpp:27"},
  {"symbol": "geo::Tagged::~Tagged", "signature": "()", "kind": "destructor",
   "location": "include/shapes.h:61", "defined_at": "src/shapes.cpp:28"},
  {"symbol": "geo::Tagged::area", "signature": "() const", "kind": "method",
   "location": "include/shapes.h:63", "defined_at": "src/shapes.cpp:31"},
  {"symbol": "geo::Tagged::label", "signature": "() const", "kind": "method",
   "location": "include/shapes.h:64", "defined_at": "src/shapes.cpp:33"}
 ],
 "fields": [{"symbol": "geo::Tagged::tag_", "location": "include/shapes.h:67"}]
}
```

Every entry carries the fields its own kind has and no others: a base has
`access`, a constructor has no signature worth reading past its parameters, and
a field has no `defined_at`, because a field is only ever declared.

---

## 4. Relationships

### `get_callers` / `get_callees`

```json
{"symbol": "geo::Circle::area", "limit": 10}
```
```json
{
 "symbol": "geo::Circle::area",
 "location": "include/shapes.h:39",
 "defined_at": "src/shapes.cpp:19",
 "kind": "method",
 "callers": [
  {"symbol": "app::measure_base_explicitly", "signature": "(const geo::Tagged &)",
   "kind": "function", "location": "src/usage.cpp:39",
   "dispatch": "virtual", "call_site": "src/usage.cpp:40"},
  {"symbol": "app::measure_circle", "signature": "(const geo::Circle &)",
   "kind": "function", "location": "src/usage.cpp:32",
   "dispatch": "virtual", "call_site": "src/usage.cpp:32"},
  {"symbol": "geo::Tagged::area", "signature": "() const", "kind": "method",
   "location": "include/shapes.h:63", "defined_at": "src/shapes.cpp:31",
   "dispatch": "virtual", "call_site": "src/shapes.cpp:31"}
 ]
}
```

`location` is where the caller *is*; `call_site` is where the call is. They
differ whenever a header declares and a source file defines.

`dispatch` is present when the call could go somewhere else at run time:
`virtual` for a virtual call, and the same marker is used for a call through a
function pointer. A caller marked `virtual` is a caller that *may* reach this
symbol, not one that certainly does — which is why the same distinction drives
`get_impact_analysis`.

If the same place calls this more than once, the entry carries `occurrences`
rather than repeating the site. A call reported through several edge kinds
carries `via`.

### `get_references`

Non-call uses: reads, writes, addresses taken, and calls made through a
function pointer.

### `get_inheritance`

```json
{"symbol": "geo::Shape"}
```
```json
{
 "symbol": "geo::Shape", "location": "include/shapes.h:16", "kind": "class",
 "derived": [{"symbol": "geo::Circle", "location": "include/shapes.h:34", "access": "public"}],
 "descendants": [{"symbol": "geo::Tagged", "location": "include/shapes.h:58"}]
}
```

`transitive` (default true) controls whether `ancestors`/`descendants` follow
the chain past the direct bases. `overrides` maps each method to what it
overrides.

### `get_symbol_dependencies`

The outgoing relationships of one symbol, grouped by kind: parameter and
return types, field types, base classes, and what it calls. This is the "what
would I have to look at to change this" query.

```json
{"symbol": "geo::Registry::operator+="}
```
```json
{
 "symbol": "geo::Registry::operator+=",
 "location": "include/shapes.h:112",
 "defined_at": "src/shapes.cpp:51",
 "kind": "method",
 "calls": [
  {"symbol": "geo::Registry::Entry::weight", "signature": "() const", "kind": "method",
   "location": "include/shapes.h:104", "defined_at": "src/shapes.cpp:44",
   "call_site": "src/shapes.cpp:52"}
 ],
 "references": [
  {"symbol": "geo::Registry::count_", "kind": "field",
   "location": "include/shapes.h:121", "call_site": "src/shapes.cpp:52"}
 ],
 "param_type": [
  {"symbol": "geo::Registry::Entry", "kind": "class",
   "location": "include/shapes.h:101", "call_site": "include/shapes.h:112"}
 ],
 "returns": [
  {"symbol": "geo::Registry", "kind": "class",
   "location": "include/shapes.h:98", "call_site": "include/shapes.h:112"}
 ],
 "dependency_count": 4
}
```

**The group names are edge kinds**, not hand-written categories — `calls`,
`references`, `param_type`, `returns`, `field_type`, `var_type`, `aliases`,
`inherits`, `overrides`, `specializes`, `instantiates`, `calls_indirect`. A
group is present only when the symbol has an edge of that kind, so an absent
`returns` means no return type was recorded, not that the key was renamed.
`dependency_count` is the exact total across every group, so a symbol whose
dependencies were cut by `limit` still reports how many there are.

A type edge carries `call_site` pointing at the *declaration* that named the
type, which is why the entries above cite `include/shapes.h` rather than the
source: the parameter was written in the header, and that is where a reader
would go to change it.

---

## 5. Files

| Tool | Answers |
| --- | --- |
| `get_file` | language, project membership, what it includes and is included by, which translation units it is compiled into |
| `get_file_symbols` | every symbol declared or defined in the file — the contents of a header without reading it |
| `get_includes` | direct or transitive includes, in both directions |
| `get_file_dependencies` | the dependency closure: every header the file reaches, and every file whose recompilation depends on it |

```json
{"path": "include/shapes.h"}
```
```json
{
 "file": "include/shapes.h",
 "is_system": false, "in_project": true, "language": "c++",
 "symbols": 50,
 "includes": ["/usr/include/c++/15/cstddef"],
 "included_by": ["src/shapes.cpp", "src/usage.cpp"],
 "translation_unit_count": 2,
 "translation_units": ["src/shapes.cpp", "src/usage.cpp"]
}
```

The include edges come from the preprocessor, so a header reached through a
macro still shows the file that was actually opened, after search-path
resolution.

---

## 6. Source, in small pieces

### `get_source_context`

```json
{"symbol": "geo::Circle::area", "context_lines": 6}
```
```json
{
 "symbol": "geo::Circle::area",
 "kind": "method",
 "declared_at": "include/shapes.h:39",
 "region": "src/shapes.cpp:13-25",
 "at": "src/shapes.cpp:19-19",
 "source": "13: \n14: const char *Shape::name() const { return \"shape\"; }\n15: \n16: Circle::Circle(double radius) : radius_(radius) { id_ = 1; }\n17: Circle::~Circle() = default;\n18: \n19: double Circle::area() const { return 3.14159265358979 * radius_ * radius_; }\n..."
}
```

The definition is used when there is one, the declaration otherwise — with the
other end reported separately (`declared_at`), so the difference is never
ambiguous. This is the tool that combines with the graph: an agent asks *who
calls this*, then asks for the twenty lines around the one it decided to look
at, and never reads a file.

---

## 7. Change and impact

### `get_impact_analysis`

Three degrees, and they are not equally certain.

```json
{"symbol": "geo::Registry::Entry::weight", "depth": 3, "limit": 10}
```
```json
{
 "symbol": "geo::Registry::Entry::weight",
 "location": "include/shapes.h:104",
 "direct": [
  {"symbol": "app::entry_weight", "signature": "(const geo::Registry::Entry &)",
   "location": "src/usage.cpp:82", "call_site": "src/usage.cpp:82",
   "reason": "calls this symbol"},
  {"symbol": "geo::Registry::operator+=", "signature": "(const Entry &)",
   "location": "include/shapes.h:112", "defined_at": "src/shapes.cpp:51",
   "call_site": "src/shapes.cpp:52", "reason": "calls this symbol"}
 ],
 "indirect": [
  {"symbol": "app::add_entry", "signature": "(geo::Registry &, int)",
   "location": "src/usage.cpp:89", "call_site": "src/usage.cpp:90",
   "reason": "calls a caller of this symbol (2 hops)", "hops": 2}
 ],
 "possible": [],
 "note": "direct: the index records this dependency. indirect: reached through the callers of the direct entries, so the effect is real but not at the first hop. possible: affected only if run-time dispatch reaches here - a virtual call, a function pointer, or a template instantiation."
}
```

| Degree | Means |
| --- | --- |
| `direct` | the index records this dependency — a resolved call, a class that derives from this type, or a declaration that names this type as a parameter, return type, field, variable or alias |
| `indirect` | reached through the callers of the direct entries, within `depth` hops; the effect is real but not at the first hop |
| `possible` | holds only if run-time dispatch reaches here: an override, a class that inherits from this type, a call through a function pointer, or a template instantiation |

Every entry carries `reason`, and the reason says which of these it is, because
"calls this symbol" and "takes this type as a parameter" call for different
reactions from a reviewer. Nothing in `possible` is claimed as affected.

A type has no callers, so asking about a class reaches the program through the
declarations that name it. That is the difference between an impact query that
answers "nothing is affected" for every class in the project and one that
answers the question:

### `get_changed_symbols`

Which symbols a commit, a range or the working tree changed.

```json
{"revision": "HEAD~1", "include_impact": true}
```
```json
{
 "revision": "HEAD~1",
 "files": 2,
 "symbols": [
  {"symbol": "geo::Circle::area", "kind": "method",
   "location": "src/shapes.cpp:19", "defined_at": "src/shapes.cpp:19",
   "changed_lines": [[19, 19]],
   "within": ["geo::Circle", "geo"]}
 ],
 "unattributed_lines": 3,
 "impact": {"direct": [...], "indirect": [...], "possible": [...]}
}
```

Each changed range is attributed to the **innermost symbol that contains it**,
with the enclosing chain in `within` — a change inside a method is also a
change to its class. Lines that fall inside nothing the index knows about are
counted in `unattributed_lines` rather than dropped: a diff over an unseen file
would otherwise appear to have changed nothing at all.

`revision` accepts a commit, a range such as `main...HEAD`, or `worktree` for
everything uncommitted (the default). `staged` restricts the working-tree
comparison to staged changes.

---

## 8. Trust

### `get_diagnostics`

Where the analysis could not see the whole translation unit.

```json
{"count": 0, "diagnostics": [],
 "note": "errors and warnings the compiler raised while indexing; constructs guarded by a failed declaration may be absent from the graph"}
```

A file the compiler rejected contributes the declarations it got through and
nothing behind the error point. This tool is how an agent distinguishes "that
symbol does not exist" from "that symbol is behind a broken build".

---

## 9. The full list

| Tool | One-line purpose |
| --- | --- |
| `get_index_status` | what the index covers and how much to trust it |
| `find_symbol` | resolve a name to the symbols it could mean |
| `get_symbol` | everything known about one symbol |
| `get_function` | a callable in detail |
| `get_class` | a type: bases, derived, members |
| `search_symbols` | substring search over names |
| `get_callers` | who calls this |
| `get_callees` | what this calls |
| `get_references` | non-call uses |
| `get_inheritance` | bases, derived, overrides |
| `get_symbol_dependencies` | outgoing relationships, grouped |
| `get_file` | one file |
| `get_file_symbols` | what a file declares or defines |
| `get_includes` | includes, either direction, optionally transitive |
| `get_file_dependencies` | the dependency closure of a file |
| `get_source_context` | a small region of source around a symbol |
| `get_impact_analysis` | what a change could affect, by degree |
| `get_changed_symbols` | what a diff changed, in symbols |
| `get_diagnostics` | compiler errors and warnings from indexing |

`--list-tools` prints this surface with its JSON Schemas, so an agent host can
discover it without a hand-written description.

---

## 10. A workflow

An agent handed an unfamiliar C++ repository, asked to change `geo::Circle::area`:

```
1.  get_index_status          -> is there an index, and does it describe this revision?
2.  find_symbol "area"        -> three candidates; which is the one?
3.  get_symbol   "include/shapes.h:39"
                              -> virtual, overrides Shape::area, 3 callers
4.  get_callers  "include/shapes.h:39"
                              -> who, and at which sites
5.  get_source_context "src/usage.cpp:32", context_lines=20
                              -> read only the caller it cares about
6.  get_impact_analysis "include/shapes.h:39"
                              -> direct, indirect, and what dispatch might reach
7.  get_class    "geo::Tagged" -> the other override, without opening the header
```

Seven calls, roughly 3 KB of JSON, no source file read in full. The equivalent
by reading source is a `grep` over the repository plus the headers it lands in,
which is where the token argument for this tool comes from.

### Using it from a client

The server speaks JSON-RPC on stdin and stdout, so anything that can spawn a
process and write to its stdin can drive it. The quickest way to see what it
exposes is MCP Inspector, which starts it and lists the tools:

```sh
npx @modelcontextprotocol/inspector astroclang mcp /path/to/project
```

That opens a browser page; `--cli` runs one request and prints the reply, which
is what a script or a terminal wants:

```sh
npx @modelcontextprotocol/inspector --cli astroclang mcp /path/to/project \
  --method tools/list
npx @modelcontextprotocol/inspector --cli astroclang mcp /path/to/project \
  --method tools/call --tool-name get_index_status
```

Spell the server command as the console script and not `python -m astroclang.cli`:
Inspector 2.8's `--cli` parser swallows `-m` (and `-c`) and ends up spawning a
bare `python`, which then fails on the JavaScript it is fed on stdin. The
console script has no such flag, and is shorter to write in a config anyway.

A host that keeps a config file wants the same command written down:

```json
{"mcpServers": {"astroclang": {
  "command": "astroclang",
  "args": ["mcp", "/path/to/project"]
}}}
```

Claude Code registers it from the command line:

```sh
claude mcp add astroclang -- astroclang mcp /path/to/project
```

If the extractor is neither on `PATH` nor in a build tree beside the package,
say where it is. Indexing needs it; querying an existing index does not:

```json
{"mcpServers": {"astroclang": {
  "command": "astroclang",
  "args": ["mcp", "/path/to/project"],
  "env": {"ASTROCLANG_INDEX": "/path/to/build/cpp/astroclang-index"}
}}}
```

Nothing is written to stdout except protocol messages. Everything else goes to
stderr — the lines naming the index it opened, and any exception raised while
answering a request — because that is where a host's log capture looks. It is
also why `astroclang mcp` run by hand prints a few lines and then appears to do
nothing: it is waiting for a client on stdin.
