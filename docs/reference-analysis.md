# Reference analysis: `code-review-graph`

This document studies [`code-review-graph`](https://github.com/tirth8205/code-review-graph)
(v2.3.9, MIT) as a reference implementation, and records which of its
architectural ideas this project adopts, which it rejects, and why.

`code-review-graph` was read, not modified. Nothing in this repository is
derived from its source; where a design idea was taken, it is credited below
and reimplemented against a different data model.

---

## 1. What `code-review-graph` does

It builds a local, persistent knowledge graph of a repository and exposes it to
coding agents over MCP, so that an agent can ask structural questions without
reading source files.

The economics are the whole point. A single question like *"who calls
`Foo::bar`?"* costs a few thousand tokens of `grep` plus file reads if answered
from source; answered from the graph it costs roughly 100–300 tokens. The
project targets *"about five tool calls and 800 tokens of graph output per
task"*.

Its architecture is one pipeline:

```
repository
   |
   v
tree-sitter parse  (per file, no compiler, no type information)
   |
   v
NodeInfo / EdgeInfo records
   |
   v
SQLite  (.code-review-graph/graph.db)
   |
   v
heuristic resolver passes  (bare-name -> qualified-name, one language at a time)
   |
   v
30 FastMCP tools  ->  coding agent
```

Everything is keyed on strings. There is no compiler, no LSP, no type system,
and no notion of a translation unit.

## 2. High-level architecture

| Layer | Implementation |
|---|---|
| Language detection | Extension table (64 extensions), then path-pattern overrides, then shebang |
| Parsing | `tree-sitter` via `tree-sitter-language-pack`, walking the CST directly rather than using query files |
| Extraction | Per-language dicts of node types driving one generic extractor |
| Model | Two dataclasses: `NodeInfo`, `EdgeInfo` |
| Resolution | A cascade of separate passes, one per language family |
| Storage | SQLite in WAL mode, string-keyed, no foreign keys |
| Serving | `FastMCP`, 30 tools, 5 prompts, stdio and streamable-http |

The code distribution tells the story: of 63,816 lines in the package,
`parser.py` alone is 19,039 (30%). The parse-and-resolve layer *is* the product;
everything else is plumbing.

Two architecture choices deserve specific credit:

**The parser never crashes the server.** Each grammar is loaded in a disposable
child interpreter first (`parser.py:572` `_run_parser_load_probe`), so a
hanging or segfaulting native grammar cannot take down the parent process. For
a tool whose whole value is availability to an agent mid-task, that is the
right instinct.

**Node and edge types are data, not code.** Per-language node-type tables
(`_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, `_CALL_TYPES`) drive a
single extractor. Adding a language is adding table rows. That is genuinely
elegant — and, as section 6 explains, it is also precisely what makes the
approach unable to express C++.

## 3. Graph and data model

**Node kinds:** `File`, `Class`, `Function`, `Test`, `Type`, plus framework
synthesised kinds (`Endpoint`, `Scheduler`, `ConfigProperty`, `Event`).

**Edge kinds:** `CALLS`, `IMPORTS_FROM`, `INHERITS`, `IMPLEMENTS`, `CONTAINS`,
`TESTED_BY`, `REFERENCES`, `DEPENDS_ON`, plus enrichment kinds for specific
frameworks.

**Identity is a path-qualified name**, not a symbol identity:

```
/abs/path/file.py                          # File
/abs/path/file.py::Class.method            # method
/abs/path/file.py::Outer.Inner.method      # nested
```

The schema is a single `nodes` table keyed by `qualified_name UNIQUE` and a
single `edges` table holding `source_qualified` / `target_qualified` **as
strings with no foreign keys** (`graph.py:197`). That denormalisation is
deliberate: it lets a resolver pass rewrite a target with one `UPDATE`, which is
what makes the multi-pass resolution strategy practical.

A derived `symbol` column (the part after `::`) exists so a dotted-tail lookup
is an indexed equality test rather than a `substr()` scan (migration v10).

**Assessment.** The string-keyed design is a good fit for dynamically typed
languages, where a name plus a file is genuinely most of the identity. It is a
poor fit for C++, where the same spelling names several distinct entities and
the same entity has several spellings. See section 7.

## 4. How it extracts semantic information

It extracts *syntactic* information and then infers semantics heuristically.

Call extraction emits a `CALLS` edge carrying whatever callee text the CST
offered. A cascade of later passes then tries to upgrade the bare name:

1. same-file symbol table lookup;
2. graph-wide bare-name match, accepted only when the candidate is in the
   call-site file **or** in exactly one file that file imports
   (`graph.py:1804`) — the docstring is explicit that global uniqueness alone is
   not evidence, because *"unrelated repositories often contain one matching
   helper by coincidence"*;
3. per-language resolvers (PHP/Rust/C#, Python via optional Jedi, Java/Spring,
   Temporal, HCL, ...).

Every edge carries a certainty grade: `confidence_tier` (`EXTRACTED` vs
`INFERRED`) and `target_resolution` (`direct` vs `unresolved`), recomputed
set-wise after each resolver pass rather than frozen at insert.

For C and C++ specifically, the extraction is:

```python
"c":   ["struct_specifier", "type_definition"]        # _CLASS_TYPES
"cpp": ["class_specifier", "struct_specifier"]
"c":   ["function_definition"]                        # _FUNCTION_TYPES
"cpp": ["function_definition", "declaration", "field_declaration"]
"c"/"cpp": ["preproc_include"]                        # _IMPORT_TYPES
"c"/"cpp": ["call_expression"]                        # _CALL_TYPES
```

Note `cpp` lists bare `declaration` and `field_declaration` as function
candidates, so every class data member matches the filter and must be rejected
downstream by a heuristic (`_cpp_is_callable_declaration`,
`_cpp_declaration_has_callable_scope`).

Overload identity is a bespoke text mangling (`_cpp_function_identity`):
`{scope}.{name}({comma-joined parameter type text}){qualifiers}`. It strips
parameter names and defaults, collapses `void`, and appends ref-qualifiers.
It is not a mangled name and not what a compiler would produce, so a call edge
can never be matched to a declaration *by signature* — only by exact type-text
equality. `size_t` and `unsigned long` do not unify; `template<typename T> void
f(T)` and `void f(int)` are unrelated strings.

## 5. How it serves context to agents

This is the strongest part of the project, and most of it is worth adopting.

**A cheap mandatory entry point.** `get_minimal_context_tool` is billed at
~100 tokens and returns status, summary, risk, affected communities and flows,
plus suggested next tools. It reports `not_ready` (with a build suggestion)
when the graph is missing or was built at a different commit.

**A three-level `detail_level` knob on 12 tools**, where `"minimal"` is a
*different code path*, not a truncation — in `get_review_context` it returns
six scalars and returns **before any source is read**.

**Hard per-list ceilings independent of the caller's request**
(`_MAX_IMPACT_NODES_SHOWN = 100`, `_MAX_IMPACT_EDGES = 150`, ...), with every
capped response reporting the untruncated total, so truncation is never silent.

**Budgets spent in priority order rather than quotas.** Review source snippets
share an 800-line pool spent in risk order; the comment is that this "buys
whole changed regions rather than a per-file line quota".

**Omission over padding.** `edge_to_dict` emits `target_resolution` only when
the value is *uncertain* — absence means the certain, common case. Impact
radius attaches one call site per impacted node plus a count, on the principle
that "the blast radius is a list of what to look at, not the full call-site
listing".

**Honesty about blindness.** `uncertainty.py` holds a `LANGUAGE_GAPS` table of
`(languages, patterns, note)`; when a result list comes back empty, one
advisory sentence — hard-capped at 140 characters so it cannot itself become a
token regression — explains what the graph cannot see. The rationale is stated
plainly: ~30 tokens of honesty replaces a multi-thousand-token grep fallback.

**Provenance on every response.** `graph_provenance` reports the stored branch
and head SHA against a live `git rev-parse`, exposing `head_matches_build`.

**Uncertainty is measured honestly.** `context_savings.py` labels its token
estimate as an approximation (`{"estimated": true}`) and offers an optional
real-tokenizer calibration, rather than presenting an approximation as fact.

## 6. Ideas worth adopting

Adopted by this project, with the change noted:

| Idea | How it is used here |
|---|---|
| Persistent local index; never re-parse per query | Same, in SQLite (`§8`) |
| Certainty as a first-class, queryable property | Per-edge `confidence` plus explicit `direct` / `indirect` / `possible` classification in impact analysis |
| Truncation always reports its total | Every bounded list carries `total` and `truncated` |
| Omission over padding in response shapes | Absent field = default/known case |
| A cheap entry-point tool | `get_project_overview` |
| Provenance stamping (branch, HEAD, head_matches_build) | Same |
| Diff → symbol mapping by line-range overlap | Same, but against exact Clang definition spans |
| Separate, shorter timeout budget for change discovery than for indexing | Same |
| Never crash the server on a parse failure | Per-TU failure isolation with recorded diagnostics |
| Boundary honesty: say what the index cannot see | `coverage` / `limitations` blocks, computed from real index statistics rather than a static table |

Two ideas are adopted in a stronger form because Clang makes them free:

- **Include resolution.** `code-review-graph` extracts `#include "foo.h"` as a
  *string* and never resolves it to a file — `_do_resolve_module` has branches
  for a dozen languages and falls through to `return None` for C/C++, with a
  test pinning that behaviour (`tests/test_multilang.py:540`). Consequently
  import-based impact analysis is inert for C/C++. Here the preprocessor
  reports the `FileEntry` each include actually opened, so include edges are
  exact, and the compilation database supplies the search paths that make them
  meaningful.
- **Macro-expanded calls.** `code-review-graph` sees no call edge through a
  function-like macro, so `CHECK(x)` expanding to `do_verify(x)` is invisible to
  caller queries, dead-code detection and impact analysis alike. Because the
  analysis here runs *after* preprocessing, a call inside a macro expansion is
  an ordinary `CallExpr` and resolves normally.

## 7. Parts unsuitable for C/C++

These are not implementation bugs to be fixed; they are consequences of the
architecture, and they are the reason this project exists.

**(a) Name-keyed identity cannot express C++.** The graph keys on a
path-qualified name. In C++ the same spelling denotes several entities
(`foo(int)` vs `foo(float)`; `A::foo` vs `B::foo`; the template and each
instantiation) and the same entity is spelled several ways (`size_t` vs
`unsigned long`; a typedef and its underlying type; a name reached through
`using`). A textual identity scheme can be extended indefinitely and will still
be approximating.

The project is candid about this: ambiguous overload sets are deliberately left
unresolved and annotated (`extra["ambiguous_targets"]`) rather than guessed, and
`resolve_cpp_scoped_call_targets` only binds "when exactly one signature-bearing
node matches" — i.e. **any overload set is left unresolved**, so `callers_of`
has no answer for an overloaded call.

**(b) No compilation configuration.** There is no `compile_commands.json`
model, no `-I` handling, no CMake/Meson/Makefile parsing, and no convention-based
include-root inference. The machinery that every other compiled language in the
project received (`tsconfig_resolver.py`, Cargo.toml, Gemfile, composer PSR4) was
never built for C++. Without include paths, `#include <vector>` cannot resolve
and neither can any project-relative include.

**(c) No preprocessor.** The only preprocessor feature modelled is `#if 0`
dead-code suppression via a parent-walk for a literal `0` condition. `#ifdef`,
`#if defined(X)`, macro-defined conditions, third-party macro families, and
above all **macro-expanded calls** are absent. A Qt project is handled by
hardcoding six `Q_*` macros and blanking them with equal-length spaces.

**(d) No translation unit.** The graph has no concept of a TU. This is mostly
harmless for tree-sitter (which parses each file once) but it means *"which TUs
see this declaration"* is unanswerable, and it makes correct incremental
re-indexing impossible for headers: a header is parsed as its own file, so
editing it does not invalidate the TUs that include it. `code-review-graph`
works around this with a two-hop `find_dependents` walk over import edges.

**(e) No virtual dispatch or overrides.** `OVERRIDES` has an impact weight in
`constants.py` but **no parser emits it** — the project's own schema doc says
so. Override-based impact propagation is therefore dead for C++, the one
language where it matters most. Function pointers, functors, `std::function`
and lambdas produce either nothing or a `REFERENCES` edge.

**(f) `.h` is a coin flip.** A header is parsed with the C++ grammar and
demoted to `c` unless C++-only syntax appears in a fixed evidence set. A header
whose only C++ features are `extern "C"` and a reference parameter is parsed as
C — and `_FUNCTION_TYPES["c"]` has no `declaration` entry, so
declared-but-not-defined member functions vanish entirely.

(**g**) Smaller gaps: `.mm` is absent from the extension table entirely despite
a comment claiming it defers to C++; `union_specifier` is a class kind for
neither C nor C++; `typedef struct {...} Foo;` produces a node in C but not in
C++.

## 8. What this project does differently

The core decision is to **stop inferring semantics from syntax and ask a
compiler instead.**

```
                       code-review-graph            astroclang
                       ------------------           ---------------
what is parsed         every file, independently    each TU, as the compiler sees it
what config is used    none                         compile_commands.json (or a reported fallback)
identity               path-qualified name          Clang USR
call targets           textual + heuristic cascade  overload-resolved by Clang Sema
overloads              left ambiguous               distinguished, and instantiation-linked
includes               unresolvable strings         resolved FileEntry + search paths
macros                 #if 0 only                   full preprocessing
virtual dispatch       not modelled                virtual/override edges
translation units      absent                       first-class, for correct incrementality
```

Concretely:

**Identity becomes a USR.** Clang's Unified Symbol Resolution already encodes
namespace, class, parameter types, template arguments and function kind. It is
stable across translation units, which is what makes deduplication of a header
declaration seen by 40 TUs correct rather than lucky. There is no ambiguity
bucket because there is no ambiguity.

**The graph gains a translation-unit layer.** Nodes and edges are recorded
*per TU* and merged for querying, with a separate occurrence table recording
which TUs saw what. That is what makes incremental re-indexing correct: editing
a header invalidates the TUs that included it, computed from the real include
graph rather than a two-hop heuristic.

**Certainty is modelled explicitly.** The spec for this project asks for
`direct` / `indirect` / `possible` to be distinguished in impact analysis.
Rather than grading a resolver's confidence, the distinction here is
*structural*: a call edge is certain (Sema resolved it), but a virtual call's
runtime target may be an override, and that is recorded as a property of the
edge and reported as *possible* impact.

**And the rejected parts:** no multi-pass resolver cascade (there is nothing to
resolve after Sema), no per-language node-type tables (the AST has types, not
tag names), and no string-keyed edges — real integer foreign keys with a USR
unique index, because the joins are now exact.

## 9. What this project kept from the reference

Honestly assessed: the *storage-and-serving* half of `code-review-graph` is
better designed than its analysis half, and that is the half worth learning
from. Specifically kept:

- the persistent-index-with-MCP shape;
- token economy as a first-class design constraint (bounded lists, totals,
  omission over padding, a cheap entry point);
- certainty and provenance as queryable properties;
- diff-to-symbol mapping by line-range overlap;
- and the habit of telling the agent what the index cannot see.

The analysis half is replaced wholesale with Clang.
