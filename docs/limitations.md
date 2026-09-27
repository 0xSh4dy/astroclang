# Limitations

What the index does not see, does not resolve, or resolves to something other
than what a reader might expect. This is the list a user needs before trusting
an answer, and it is written to be read against
[`semantic-model.md`](semantic-model.md), which says what the model *is*.

Every entry here is a known boundary, not a suspected bug. Where a claim is
checkable it is stated so that it can be checked.

---

## 1. Analysis is per translation unit

`astroclang-index` analyzes one translation unit at a time and reports what *that*
compilation saw. Nothing is merged at analysis time.

**A declaration whose definition lives in another translation unit is a
stub.** It has an identity, a name, a location and a signature, but no body, so
no `calls` edge originates from it. On a project like googletest this is not a
corner case: roughly half of all call edges land on a stub. The alternative —
whole-program analysis — costs a link step's worth of memory on top of an
already memory-bound front end, and it is not what an agent asking "who calls
this" needs. The share is measured in [`evaluation.md`](evaluation.md) §6
rather than left to be discovered.

**Macro uses are not recorded at all.** A `#define` is indexed as a node with
a name and a definition site; nothing records where it is expanded. So
`get_references("CLIB_MAX_ITEMS")` returns an empty list for a macro that is
used on two lines of the fixture, and `get_callers` says "nothing in the index
calls this" — a statement that is true of a table that was never populated for
macros and reads as "this macro is unused".

Part of that is deliberate and tested: `CLIB_SQUARE(3)` is gone by the time an
AST exists, and the index must not invent a `calls` edge to a macro as if it
were called. `python/tests/test_semantics.py` asserts that no edge of any kind
reaches a macro node.

But an empty answer still reads as "unused", and that reading is dangerous
enough to have been worth a special case: for a macro, both tools now return a
note saying the empty list means the index does not track macro uses, rather
than leaving a reader to conclude a live macro is dead. Recording the expansion
sites is the obvious next feature — the preprocessor reports them, and
attributing a site to its enclosing function is what the span machinery in
`query.py` already does for changed lines — but it is not implemented, so the
answer says so instead of guessing.

What the macro *expands to* is correspondingly unmodeled: a call written inside
a macro body is not attributed to anything.

**Conditional compilation follows the configuration given.** With a
`compile_commands.json`, that is the real configuration and the answer is
exact. Without one, `astroclang-index` falls back to a permissive guess, and
`get_index_status` reports `accuracy: degraded` with the count of translation
units analyzed that way — because an index that silently guessed is worse than
one that says it guessed.

---

## 2. Run-time dispatch is labeled, never resolved

**A virtual call resolves to the declaration the compiler selected, which is
the static one.** The callers of `Shape::area` are the call sites that
dispatched through `Shape`. A call that reaches `Tagged::area` at run time
because the object was a `Tagged` is recorded against `Shape::area` and the
override is reported separately, under `possible`, with a reason.

**A call through a function pointer is not resolved to any function.** It is
recorded as `calls_indirect` against the *variable* being called, and the
functions whose address was taken are recorded as `references`. A reader can
follow that chain; the index does not pretend the target is known.

**`std::function`, member pointers and callbacks held in data structures** are
in the same bucket as function pointers: the target is a value, and values are
not tracked.

---

## 3. Types

**A name the author wrote outranks what it expands to.** A field of type
`clib_visit_fn` records an edge to `clib_visit_fn`, not to the `clib_point`
that happens to appear in the callback's signature. Following the alias onward
is what the `aliases` edge is for. This is deliberate — see
[`semantic-model.md`](semantic-model.md) §6 — but it means a reader asking
"what is this field really" has to take one more step than they might expect.

**A function-pointer typedef's `aliases` edge names the types in its
signature.** `typedef int (*clib_visit_fn)(struct clib_point *, void *)` yields
an `aliases` edge to `clib_point`. There is no declaration for "pointer to
function returning int" to point at, so the edge lands on the declarations the
signature mentions. It is a *mentions* relationship wearing the name of an
*is* relationship. No test asserts otherwise, and the honest fix — emitting no
edge when a type has no declaration — is not obviously better, because then
the alias would appear to name nothing at all.

**An anonymous struct declared inside a typedef takes its display name from
the typedef.** `typedef struct { ... } clib_size;` produces two symbols, the
`struct` and the `typedef`, both named `clib_size`, at different locations.
They are distinct by USR and `find_symbol` returns both as candidates with
their locations, so the ambiguity is reported rather than guessed — but a
reader looking at a raw edge list sees `clib_size -> clib_size` and has to
check the USRs to learn it is not a self-edge.

**Depth is bounded.** `addTypeEdges` walks a `QualType` to `--max-type-depth`
(default 4) and stops. A type nested deeper than that contributes the edges
along the way and nothing past the bound.

**Template instantiations are recorded where the compiler formed them.** An
instantiation links to its pattern (`instantiates`) and a specialization to
its template (`specializes`), so an impact query for `std::vector` reaches
users that only ever wrote `std::vector<int>`. What is *not* attempted is
predicting instantiations that were never formed in an indexed translation
unit.

---

## 4. Scope defaults

**Function-local variables are not indexed.** A local is not addressable from
outside its function, so it cannot be a dependency of anything else. The flag
exists for callers who want them.

**Parameters and non-type template parameters are not indexed as nodes**
unless asked for; they appear in signatures and as `param_type` edges.

**System headers are indexed but excluded from lists by default.** A symbol in
`/usr/include` is present in the graph — it has to be, or a call into the
standard library would resolve to nothing — but `include_system` defaults to
false so that an answer is about the project. `get_index_status` separates
`symbols` from `symbols_in_project` for the same reason.

---

## 5. What is not attempted at all

* **Other languages.** The architecture separates analyzer from model from
  query layer, and the analyzer is the only place Clang appears, so a second
  language would be a second analyzer rather than a rewrite. None is
  implemented, and none is planned for the sake of completeness: the target is
  C and C++ quality.
* **Whole-program or link-time analysis.** See §1.
* **Semantic diffing of two revisions.** `get_changed_symbols` reports what a
  diff changed and what that could affect. It does not attempt to say whether
  a change was safe, correct, or a behavior change — the semantic index is the
  foundation for that, and the foundation is what this tool is.
* **Cross-translation-unit macro or include-path reasoning.** The include graph
  comes from the preprocessor, so it is exact for the configuration given; it
  is not projected onto a configuration that was never compiled.

---

## 6. Index trust

`get_index_status` is the tool that answers "how much should I believe this",
and it is meant to be called first:

| Field | Means |
| --- | --- |
| `accuracy` | `exact` (every translation unit had a compilation database), `degraded` (some were analyzed with a fallback), `unknown` (the index was built without recording a configuration) |
| `degraded_tus` | how many were analyzed without a compilation database |
| `failed_tus` | how many reported compiler errors, so declarations behind the error point are absent |
| `diagnostics` | `get_diagnostics` has the actual messages |
| `built_at` and the recorded revision | whether the index still describes the working tree |

A translation unit the extractor could not produce facts for *at all* — a crash
or a missing binary — is a third thing again: the indexer's own report counts
it as `failed` and keeps the previous entry for that file rather than replacing
a complete result with a partial one.
