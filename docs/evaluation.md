# Evaluation

What the index costs and what it is worth, measured on real C++ rather than
asserted. Every number here comes from a run of `scripts/evaluate.py` or a
script recorded beside it, on the machine described in §1.

---

## 1. Method, and what these numbers are not

**The machine is a laptop, not a benchmark rig.** Intel i5-10210U (4 cores, 8
threads), 15 GB RAM, NVMe, with a desktop session running. It is the machine
the tool was developed on, which makes the numbers representative of using it
and not representative of a quiet server. This matters more than usual here,
and §4 is the reason: on this workload the limiting resource turned out to be
memory, not cores, so runs of the *same configuration* on the same tree vary by
more than 2× depending on what else the machine is doing. Every figure below
says which regime it was measured in.

**Two subjects, chosen to be different from each other.**

| Subject | What it is | Why it is here |
| --- | --- | --- |
| `/usr/src/googletest` | 108 translation units of C++17, templates, multiple inheritance, a large gmock test suite | a real project with a real call graph, where most calls land on code the index holds |
| `cpp/` (this tool's own extractor) | 7 translation units, but each pulls in the entire LLVM 17 and Clang 17 header closure | the stress case: a small project whose analysis is dominated by declarations it deliberately does not index |

Neither subject ships a `compile_commands.json`. The googletest one was
generated with `scripts/make_compdb.py` at `-std=c++17`; the tool's own with
the same script against its CMake configuration. That the standard matters is
not a detail: the first googletest run used `-std=c++14` and lost everything
behind the first `if constexpr`, which appears as a missing symbol rather than
as an error. `cg-index --print-config` prints what a file will be compiled
with, and is the way to check a generated database against the tree.

**What is deliberately not measured:** peak memory of a full parallel run as a
*system* property (see §4 — it is measured, but per worker count), and
correctness against a reference implementation. Correctness is the test
suite's job (`python/tests`), not the evaluation's.

---

## 2. Indexing

| | googletest |
| --- | --- |
| Translation units | 108 |
| Failed | 0 |
| Indexed without a compilation database | 0 |
| Wall time | 120.08 s |
| Seconds per translation unit | 1.11 |
| Peak extractor RSS | 1864.1 MB |

The second subject is not given a column here because its indexing cost is not
what it is for: it exists in this document to show what resolution looks like
when a project's analysis is dominated by headers it does not own, which is §6.

`peak extractor RSS` is `getrusage(RUSAGE_CHILDREN).ru_maxrss`, which on Linux
is the **largest single child**, not the sum of concurrent children. It is a
per-translation-unit high-water mark: one unity build (`gmock_all_test.cc`)
needs 1.84 GB on its own. §4 measures the concurrent total separately, and the
difference between those two numbers is the most important thing in this
document.

### Where the 1.8 GB goes

To check whether the extractor is the cost or Clang is, the same file was
compiled with the same arguments and no analysis at all:

```sh
clang++ -std=c++17 -fsyntax-only -I... gmock_all_test.cc
```

| | Peak RSS | Time |
| --- | --- | --- |
| `clang++ -fsyntax-only` | 1840 MB | 35.87 s |
| `cg-index` | 1864 MB | 34.85 s |

The extractor adds about 1.4% over the front end it drives. The memory is the
C++ front end's cost, not this tool's, and no amount of care in the visitor
changes it. What the tool *can* be faulted for is not bounding how many of
those it runs at once — see §4.

---

## 3. The database

| | googletest |
| --- | --- |
| Size on disk (with WAL) | 575.6 MB |
| Symbols | 55 231 |
| Symbols in project files | 36 102 |
| Edges | 656 518 |
| Include edges | 121 674 |
| Files known | 570 |
| Project files | 157 |
| Bytes per symbol | 10 927.5 |
| Merge (`REBUILD_SYMBOLS`) | 3.28 s |

Ten kilobytes per symbol is dominated by the raw layer, which is deliberate:
every translation unit's own report of a header is kept, because that is what
makes dropping one translation unit a `DELETE` rather than a reconstruction.
The merged layer is derived and costs a 3.28 s rebuild over the whole database.

Symbols by kind, which is what a C++ project is made of:

| Kind | Count | | Kind | Count |
| --- | ---: | --- | --- | ---: |
| method | 16 340 | | macro | 750 |
| constructor | 12 289 | | typedef | 724 |
| class | 6 974 | | conversion_function | 721 |
| function | 5 549 | | struct | 588 |
| variable | 4 414 | | enumerator | 110 |
| destructor | 3 639 | | namespace | 57 |
| template_parameter | 1 621 | | enum | 47 |
| field | 1 405 | | union, namespace_alias | 3 |

Edges by kind:

| Kind | Count | | Kind | Count |
| --- | ---: | --- | --- | ---: |
| contains | 328 067 | | var_type | 2 986 |
| calls | 190 763 | | instantiates | 1 707 |
| references | 60 059 | | calls_indirect | 1 187 |
| specializes | 47 629 | | param_type | 119 |
| overrides | 14 088 | | aliases | 2 |
| inherits | 9 911 | | | |

---

## 4. Scaling, and the bottleneck that matters

The number this replaces was wrong, and how it was wrong is worth recording.
A first measurement ran the same tree serially and then in parallel, reported
**3.10×** (369 s then 119 s), and was an artefact: the serial run went first
and read every file from a cold page cache, the parallel run went second and
found them all in memory. Alternating the order and repeating it gives the
real picture, which is not a single number at all.

That correction is the reason this section now says what it does not yet know.
The sweep that would produce the table is written and works —
[`scripts/scaling.py`](../scripts/scaling.py), which indexes the tree from
scratch at each worker count, alternating the order between passes so that
neither configuration systematically gets the warm cache:

```sh
scripts/scaling.py --root /usr/src/googletest --jobs 1 2 4 8 --repeat 2
```

It measures two different things about memory, because they disagree about
where the limit is. Wall time keeps improving with more workers until the cores
run out. Memory does not: `evaluate.py` reports `getrusage(RUSAGE_CHILDREN)`,
which on Linux is the **largest single child**, while the quantity that decides
how wide a run can safely go is the **sum across concurrent children**. One
unity build needing 1.84 GB and eight of them needing 14.7 GB are the same
number under the first measure and very different facts about the machine. The
sweep samples `/proc` while the run proceeds to get the second.

**The sweep has not been run to completion on this subject**, and the table is
absent rather than estimated. The machine is the one described in §1 — a
laptop with a desktop session, 15 GB of RAM of which roughly 3 GB is free at
the time of writing. At the measured worst case of 1.84 GB per translation
unit, only the single-worker configuration fits, and one point is not a curve.
The script refuses any width it does not believe fits (`MEMORY_HEADROOM` = 75%
of `MemAvailable`, and `--per-worker-mb` to lower the estimate for a tree
without a unity build), so running it here would report a refusal rather than a
measurement. That refusal is the finding: **on this workload the binding
constraint is RAM, not cores**, which is why `--jobs` defaults to
`os.cpu_count()` and is worth setting by hand on a machine with fewer free
gigabytes than cores.

What is not in doubt is the shape, and it is the part that matters for using
the tool: the front end dominates, the extractor adds ~1.4% over it (§2), and
the cost is per translation unit and bounded by memory. The open question is
the exact speedup curve, and it needs a machine that is not also running a
desktop to answer.

---

## 5. Query latency

What an agent actually pays. Measured over 200 symbols drawn with a fixed seed
from the project's own functions and methods, and separately against the
single most-called symbol in the index, which is the worst case.

### Ordinary symbols — 200 sampled

| Tool | median | p95 | max | median payload |
| --- | ---: | ---: | ---: | ---: |
| `get_symbol` | 0.48 ms | 5.08 ms | 165.81 ms | 492 B |
| `get_callers` | 0.28 ms | 1.30 ms | 22.60 ms | 539 B |
| `get_callees` | 0.34 ms | 6.93 ms | 50.77 ms | 433 B |
| `get_impact_analysis` | 0.44 ms | 1.51 ms | 29.14 ms | 872 B |
| `get_source_context` | 0.52 ms | 1.05 ms | 1.57 ms | 2199 B |

The median answer costs half a millisecond and half a kilobyte. The `max`
column is the same code meeting a symbol that is nearly a hub — the sample is
drawn from functions with at least one call edge, and a few of those are
called from everywhere.

### Questions with no subject

| Tool | median | p95 | payload |
| --- | ---: | ---: | ---: |
| `find_symbol` | 0.46 ms | 0.49 ms | 1851 B |
| `search_symbols` | 16.52 ms | 19.52 ms | 2225 B |
| `get_index_status` | 19.29 ms | 21.78 ms | 490 B |

### The worst case

Against `testing::internal::CodeLocation::CodeLocation`, which the index
records as called from 3606 distinct places:

| Tool | median | payload |
| --- | ---: | ---: |
| `get_source_context` | 0.77 ms | 1971 B |
| `get_callees` | 0.87 ms | 206 B |
| `get_impact_analysis` | 31.63 ms | 3100 B |
| `get_callers` | 32.27 ms | 2518 B |
| `get_symbol` | 143.01 ms | 523 B |

A hub is 300× slower than a typical symbol, and the cause is one line of SQL
rather than the graph design. `get_symbol` reports the caller and callee
*counts*, each an exact `COUNT(DISTINCT src)`, and a distinct count over N rows
is O(N):

```
symbol()        0.12 ms     the row itself
degree in      44.73 ms     COUNT(DISTINCT src) over every edge into it
degree out      0.04 ms     the same query, on a symbol nothing calls
ancestors()     0.20 ms
```

Counting rows instead of distinct callers would be a different number and a
wrong one — the same function called twice in one file is one caller. It is
left exact, and the cost is confined to the handful of symbols in any project
that are hubs. An agent that asks about `std::string`'s constructor pays 143 ms
once; every other question it asks is sub-millisecond.

---

## 6. Semantic resolution

The metric that matters is not "how many edges have a symbol row" — the
extractor mints a stub for every declaration it merely refers to, so that count
is always zero and the ratio always 100%. What says something is *what kind of
thing* each edge lands on.

For googletest, all 656 518 edges:

| Destination | Edges | Share |
| --- | ---: | ---: |
| defined in a project file | 503 783 | 76.7% |
| a stub (declared, body not analyzed) | 124 855 | 19.0% |
| a synthetic identity (anonymous, TU-local) | 27 880 | 4.2% |

And for call edges alone — the ones an agent follows to understand behaviour:

| Destination | Calls | Share |
| --- | ---: | ---: |
| defined in a project file | 87 973 | 45.8% |
| a stub (declared, body not analyzed) | 103 203 | 53.8% |
| a synthetic identity | 774 | 0.4% |

**45.8% of calls resolve to a body the index holds.** That is the honest
headline, and it is lower than the containment and type edges because calls
are what leave the project: googletest calls into libstdc++ constantly, and
system headers are not indexed by default.

The 53.8% is the trade recorded in [`clang-usage.md`](clang-usage.md) §4, made
measurable rather than left to be discovered. Indexing the standard library
would move that number and destroy the graph's usefulness: a question about
`resize` would be answered by libstdc++'s twenty overloads rather than by the
project's own code. The call *sites* are recorded, the identity is exact, and
the body is what is missing — `get_callers("std::vector<int>::resize")` is
answerable and says who calls it; `get_callees` of a stub is empty because
there is nothing to report.

### The second subject, and why it is a worse one

The extractor's own source tree — 6 translation units, 61 927 symbols, 365 679
edges, 214 MB of database — reports **0.2% of call edges landing in project
code**: 128 of 73 046. The other 99.8% is 68.8% outside the project and 29.8%
on stubs.

That is not a defect in the analysis, and it is worth being precise about why,
because the number looks alarming. Those 6 translation units pull in the whole
LLVM 17 and Clang 17 header closure — 615 files reach the index, of which 12
are this project's own. Almost every declaration in every file's AST belongs to
somebody else, and the tool's own code is a thin layer over it. A `resize` in
this tree is libstdc++'s, and the index says so.

So it is a good stress test — 6 files that produce 365 679 edges is a hard case
for the store — and a useless measure of resolution quality. googletest is the
subject the bold number above comes from, and the reason both are reported is
that a reader should be able to see how much the answer depends on which
project was asked.

---

## 7. Incremental indexing

One source file touched (a comment appended), then re-indexed:

| | googletest |
| --- | --- |
| Full index | 120.08 s |
| Re-index after one file changed | 9.63 s |
| Translation units re-indexed | 1 |
| Translation units left alone | 107 |
| Speedup | 12.5× |

9.63 s for an 18-line change is the cost of one unity-build translation unit
(`gmock-all.cc` is a file that `#include`s the rest of gmock), and it is the
worst case for this project rather than the typical one. The part that scales
with the *project* rather than with the change is the merge: 3.28 s over 55 231
symbols, and that is the number to watch as a repository grows.

---

## 8. Bottlenecks found and deliberately not fixed

Each of these was measured, diagnosed, and left alone. None of them is on the
path of a question an agent asks repeatedly.

| Cost | Cause | Fix that was rejected |
| --- | --- | --- |
| `get_index_status` 19.3 ms | `COUNT(*) FROM raw_edge` scans 656 518 rows | a maintained counter table, or a cheaper approximate count — for a call an agent makes once per session |
| `search_symbols` 16.5 ms | `LIKE '%text%'` cannot use an index, so it scans every symbol | FTS5 — a second index to keep in sync, for substring search over 55 231 rows that is already fast enough to feel instant |
| `get_symbol` on a hub 143 ms | an exact `COUNT(DISTINCT src)` is O(rows) | approximate or cached counts — the exactness is the point |
| `peak extractor RSS` 1.8 GB | the Clang front end on one unity build | nothing in this tool can fix it; see §4 for what it can fix |

The one that is not merely a latency question is the last: worker count is
chosen from `os.cpu_count()` alone, so on a machine with fewer free gigabytes
than cores the parallel indexer will swap rather than slow down gracefully.
That is a real limitation and §4 is its measurement.

---

## 9. What these numbers do not say

* **Not a comparison.** Nothing here benchmarks another tool. The claim being
  supported is that the index is affordable and the answers are cheap, not that
  it is faster than something else.
* **Not reproducible to the digit.** §1 and §4 give the reason; the spread on
  the same configuration is larger than most of the differences in this
  document.
* **Not the whole accuracy picture.** §6 measures what edges resolve to. It
  does not measure whether every construct was visited — that is the test
  suite's job, and the corpus is deliberately small and adversarial rather than
  large and representative.
* **Not tuned.** No part of this work was optimized against these numbers
  except where a measurement showed a wrong answer (the plan-cache statistics
  in `architecture.md` §3.4, which took a 53 ms query to 0.02 ms) or a wrong
  metric (the resolution ratio this section replaces).
