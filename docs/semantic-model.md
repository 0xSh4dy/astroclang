# The semantic model

Every node and edge the index holds, what it means, and what it is derived
from. The extractor emits these; the store merges them; the query layer reads
them.

---

## 1. Identity

A node is identified by a **USR** — Clang's Unified Symbol Resolution string,
produced by `clang::index::generateUSRForDecl`. It is the identity, and
everything else in this document depends on it.

A USR is a mangled, structural encoding of a declaration that already
distinguishes exactly the cases a name cannot:

```cpp
double scale(double);        // c:@F@scale#d#
int    scale(int);           // c:@F@scale#i#
A::foo();                    // c:@S@A@F@foo#  (say)
B::foo();                    // c:@S@B@F@foo#
template <typename T> T twice(T);   // c:@FT@>1#Ttwice#t0.0#
```

Functions, methods, fields, types, namespaces, templates and their
instantiations all have distinct USRs, and a redeclaration has the *same* USR
as the declaration it redeclares — which is what lets the merge collapse a
header seen by fifty translation units into one node. `Indexer::usrOf` resolves
to the canonical declaration before generating, because that is the one that
carries the shared identity.

Two cases need a fallback:

* **Anonymous entities** — an unnamed struct, a lambda's closure type — have no
  USR. `usrOrSynthetic` mints a translation-unit-local identity so they still
  participate in the graph. These are the symbols the evaluation reports as
  `synthetic_identity`.
* **Declarations Clang declines to give a USR at all** fall back to the same
  mechanism.

Every tool reports symbols as `file:line` and accepts that spelling back, so a
USR is never the way one answer is handed to the next question. The three tools
that describe a single symbol (`get_symbol`, `get_function`, `get_class`) do
report it, because it is the one string that names that symbol and nothing else
— which is what a reader wants when citing a finding — but no tool requires one
back.

---

## 2. Node kinds

`Indexer::kindOf` classifies a declaration. This is the complete set:

| Kind | Source | Notes |
| --- | --- | --- |
| `namespace` | `NamespaceDecl` | containment only |
| `namespace_alias` | `NamespaceAliasDecl` | |
| `class` | `RecordDecl::getKindName()` | `class`, or from a `ClassTemplateDecl` |
| `struct` | `RecordDecl::getKindName()` | |
| `union` | `RecordDecl::getKindName()` | |
| `enum` | `EnumDecl` | |
| `enumerator` | `EnumConstantDecl` | |
| `function` | `FunctionDecl`, `FunctionTemplateDecl` | |
| `method` | `CXXMethodDecl` | |
| `constructor` | `CXXConstructorDecl` | |
| `destructor` | `CXXDestructorDecl` | |
| `conversion_function` | `CXXConversionDecl` | `operator int()` |
| `field` | `FieldDecl` | |
| `variable` | `VarDecl` | globals by default, locals with `--locals` |
| `typedef` | `TypedefNameDecl` | covers `typedef` and `using`; the `using` flag distinguishes them |
| `template_parameter` | `TemplateTypeParmDecl` and friends | |
| `macro` | preprocessor | the definition site; not an AST node |

## 3. Node fields

| Field | Meaning |
| --- | --- |
| `usr` | identity (§1) |
| `kind` | §2 |
| `name` | the short name as written |
| `qualified` | `geo::Circle::area` — the spelling a reader would use |
| `signature` | the parameter list, `(double, int)` |
| `type_text` | the return type for a function; the declared type for a variable, field or typedef |
| `file_id`, `line`, `col` | where the declaration is |
| `def_file_id`, `def_line`, `def_col` | where the definition is, when it differs |
| `end_line`, `end_col` | the end of the definition, which is what makes a source region possible |
| `parent_usr` | the enclosing namespace, class or function |
| `tu_count` | how many translation units mention it — a cheap proxy for how widely used it is |
| `stub` | 1 when this is a name and a location with no analyzed body (§5) |
| `flags` | §4 |

## 4. Flags

Recorded as a JSON object on the node, because they are properties of the
declaration rather than relationships to anything.

| Flag | On | Meaning |
| --- | --- | --- |
| `def` | any | this declaration is the definition |
| `tmpl` | any | templated (a member of a template counts) |
| `inst` | any | an implicit or explicit instantiation |
| `spec` | any | an explicit specialization |
| `impl` | any | compiler-generated, not written in the source |
| `sys` | any | located in a system header |
| `deprecated` | any | |
| `anon` | record | anonymous struct or union |
| `abstract` | record | has a pure virtual member. **Absent for a forward declaration**, because the AST has no answer for one and "not abstract" would be a claim it cannot back |
| `polymorphic` | record | has a virtual member |
| `lambda` | record | a closure type |
| `union` | record | |
| `deleted`, `defaulted`, `variadic`, `inline`, `constexpr`, `extern_c` | function | |
| `static` | function, method, variable | internal linkage: a free function declared `static`, anything in an anonymous namespace, or a static local. Two translation units may each have their own `clamp` and they are not the same function |
| `virtual`, `pure`, `override`, `const`, `explicit` | method | `override` means `size_overridden_methods() > 0`, which is the set impact analysis expands virtual dispatch through |
| `using` | typedef | `using X = Y` rather than `typedef Y X` |
| `scoped`, `fixed` | enum | `enum class`, `enum class E : int` |
| `access` | members | `pub`, `prot`, `priv`. Omitted for parameters and template parameters, whose access level Clang reports but which a reader would not mean by the word |

## 5. Stubs

A stub is a node with a name, a kind and a location, and no analysis behind it.
It is created by `Indexer::reference()` when something refers to a declaration
the extractor did not traverse — a function in libstdc++, a header that was not
on the include path.

Stubs are what make `get_callers("std::vector<int>::resize")` answerable
without indexing libstdc++: the call sites are recorded, the identity is right,
and only the callee's body is missing. What is *not* claimed is that the index
knows anything about it, which is why the evaluation reports the share of edges
landing on stubs instead of counting edges with no symbol at all (that count is
always zero, and means nothing).

A stub is upgraded in place if the same USR is later reached as a full
declaration.

---

## 6. Edge kinds

| Edge | Meaning | Emitted from |
| --- | --- | --- |
| `calls` | this function calls that one, resolved | `CallExpr::getDirectCallee()` |
| `calls_indirect` | a call through a function pointer, member pointer or `std::function` | the callee *expression*, when there is no direct callee |
| `references` | a non-call use: a read, a write, an address taken | `DeclRefExpr`, `MemberExpr` |
| `contains` | the containment hierarchy of §7 | every indexable declaration |
| `inherits` | a class derives from a base | `CXXRecordDecl::bases()` |
| `overrides` | this method overrides that one | `CXXMethodDecl::overridden_methods()` |
| `specializes` | this declaration specializes that template | `getSpecializedTemplate()` |
| `instantiates` | this instantiation came from that pattern | implicit instantiation of a class or its base |
| `returns` | the function's return type | the return `QualType` |
| `param_type` | a parameter's type | each parameter's `QualType` |
| `field_type`, `var_type` | a field's or variable's type | the `ValueDecl`'s `QualType` |
| `aliases` | a typedef or alias and the type it names | `getUnderlyingType()` |

Every edge carries a source location and a weight.

### Aggregation

Edges are folded per `(kind, source, target, source-file)` **within one
translation unit**. A function called two hundred times in a loop produces one
edge with `weight = 200` and one call site, not two hundred records. The file
is part of the key because the same call written in a header is a different
fact from a call written in the `.cpp`, and a reader wants to know which.

Across translation units, nothing is folded at analysis time — the raw layer
holds each unit's report separately, and the query layer counts **distinct**
sources and targets rather than summing rows. Counting rows would report the
size of the build rather than the number of relationships.

### Type edges

Type edges are the reason the graph can answer "what would I have to look at to
change this". `addTypeEdges` walks a `QualType` structurally — through
pointers, references, arrays, member pointers, `auto`, `decltype`, elaborated
types, substituted template parameters and function prototypes — down to the
named declarations at the bottom, and emits one edge per named type reached.
Depth is bounded (`--max-type-depth`, default 4) and visited types are tracked,
so a recursive type does not loop.

Two decisions in that walk are worth stating, because both are places where the
obvious implementation is quietly wrong.

**A name the author wrote outranks what it expands to.** `getAs<T>` on a
`QualType` desugars before it answers, so a question asked in the wrong order
gets a true answer to a different question. A field declared `clib_visit_fn`
holds a callback; the fact that the callback's signature mentions
`struct clib_point *` does not make the field a `clib_point`. The typedef is
therefore tested first and wins, and the typedef's own `aliases` edge is what
carries a reader onward to the underlying type. The same rule covers `using`
aliases, which reach the same node.

**The cycle guard is keyed on the type node, not on its canonical form.** Those
differ exactly where this walk does its work: a type written in source is
usually sugar over the declaration it names, so `Base` arrives as an
`ElaboratedType` whose canonical type is the `RecordType` it wraps. Keying the
guard on the canonical form inserts that `RecordType` at entry, and the
unwrapping step then arrives at the very same `RecordType` and is turned away
as a cycle — so the named declaration is never reached and *no edge is
emitted*. Every node in a sugar chain is a distinct object and every structural
child is strictly smaller, so keying on the node itself terminates just as
well. This bug is worth naming because of its shape: it produced no error, no
warning, and no missing symbol — only a graph with the type edges missing,
which every test that checked symbols rather than edges would have passed.

---

## 7. Containment

```
Namespace  geo
    |
    +-- Class  geo::Shape
    |     +-- Field        geo::Shape::id_
    |     +-- Method       geo::Shape::area
    |     +-- Constructor  geo::Shape::Shape
    |     +-- Destructor   geo::Shape::~Shape
    |
    +-- Class  geo::Circle
          +-- Method       geo::Circle::area     -- overrides --> geo::Shape::area
```

`contains` is emitted for every indexable declaration, from its enclosing
context. It is what `get_file_symbols` and `get_class` read, and it is how a
changed line is attributed to the innermost thing that contains it.

---

## 8. Files and includes

A `File` node is a path, interned globally by `Store.file_id` after
normalization (`..` removed, symlinks followed, relative paths resolved against
the project root). Two flags:

* `is_system` — Clang says the file is a system header.
* `in_project` — the path is under the project root.

`in_project` is what keeps every answer about this codebase about *this*
codebase: the default for every query is to report project symbols only,
because the alternative is that a question about `resize` is answered by
libstdc++.

Include edges are `file --includes--> file`, with the line, whether the include
was angled, and the spelling as written. They are recorded from the
preprocessor's own view of which file opened which, so an include reached
through a macro still shows the file it actually opened. They are what
`get_includes` and `get_file_dependencies` traverse.

---

## 9. What the model does not claim

The design rule throughout is that a fact the AST does not support is left out
rather than approximated:

* A call through a function pointer is `calls_indirect`. It names the *variable*
  being called, not a guess at what it holds.
* A dependency that holds only under run-time dispatch is reported by
  `get_impact_analysis` as **possible**, with the reason, never as `direct`.
* An overloaded name that three declarations share comes back as three
  candidates from `find_symbol`, not as one.
* An abstract/polymorphic flag is absent on a forward declaration, because the
  AST has nothing to say about it there.
* A macro call is not a call. Macros leave no trace in the AST after
  preprocessing; what is recorded is the definition site.
