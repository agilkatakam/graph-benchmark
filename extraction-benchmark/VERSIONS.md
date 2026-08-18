# Exact environment the published numbers were produced on

**Why this file exists.** Graphify's score is `f(its graph.json, our truth, our scorer)`. Our
truth comes from referees that shell out to external toolchains, so **a different `ctags` or a
different `tsc` changes truth, and Graphify's recall moves even though Graphify did not.** Nothing
here is optional if you want the published digits.

---

## The system under comparison

| | version | how to get exactly this |
|---|---|---|
| **Graphify** | `graphifyy` **0.9.28** | `pip install "graphifyy @ git+https://github.com/Graphify-Labs/graphify@v0.9.28"` — upstream tag `v0.9.28`, commit `1644230`. The PyPI name is `graphifyy`, not `graphify`. |
| Invocation | `graphify update <repo> --no-cluster` | The zero-LLM path, and the invocation Graphify's own README documents. Verified: `--no-cluster` changes **nothing** on the declaration planes — on leveldb the C/C++ node sets are byte-identical with and without it. |

Graphify was run **unmodified**. The checkout used carried no local changes.

## The referees — these decide truth, and therefore Graphify's recall

| language | referee | dependency | version here |
|---|---|---|---|
| Java (+ all second referees) | `ast-referee.py` | `tree-sitter` python bindings and per-language grammars | see `requirements.txt` (tree-sitter 0.25.2, tree-sitter-java 0.23.5, …) |
| TypeScript, JavaScript | `ast-referee-tsc.js` | `typescript` npm package | **5.2.2** — `npm ci` in `referees/`, which ships a lockfile. `TSC_REFEREE_PATH` overrides. TypeScript 7 ("tsgo") exports no JavaScript AST API and cannot be used. |
| Python | `ast-referee-cpython.py` | CPython — `ast` node types change between minor releases | **3.13.9** |
| C# | `ast-referee-roslyn/` | .NET SDK + `Microsoft.CodeAnalysis.CSharp` | dotnet **10.0.400**, package version in `referee.csproj` |
| SQL | `ast-referee-sqlref.py` | `pglast` (pins a libpg_query/PostgreSQL major), `sqlglot`, `sqlfluff`, ScriptDom | pglast **8.4**, sqlglot **30.17.0**, sqlfluff **4.3.0** |
| C/C++ | `ast-referee-ctags.py` | **Universal Ctags** | **6.2.1** |
| Go | `ast-referee-goast/` | Go toolchain (`go/parser`, `go/ast`) | **go1.26.6** |
| PHP | `ast-referee-phpast.php` | PHP + `ext/ast` | PHP **8.5.9**, ast **1.1.3** |
| Kotlin | `ast-referee-ktpsi/` | `kotlin-compiler.jar` | kotlinc-jvm **2.4.10** |
| (node scripts) | | Node.js | **v22.23.0** |

**Universal Ctags is the highest-risk pin in the set.** It adjudicates ~62,000 C/C++
declarations, its C/C++ parser changes between releases, and C/C++ is already the weakest
language on both sides. If your ctags differs from 6.2.1, expect the C/C++ row to differ and do
not compare it against the published one. The referee warns on a version mismatch and **aborts**
if Universal Ctags is absent entirely: BSD `ctags` (which is `/usr/bin/ctags` on macOS) produces
an empty truth set and exits 0, which would read as a 0.0% cell for both systems rather than as
a failure.

**Every referee stamps `referee_version` into its truth JSON.** A published number with no
provenance is not reproducible, and a toolchain mismatch is otherwise silent. What each one
records, as measured on the machine that produced the published figures:

| referee | `referee_version` |
|---|---|
| `ast-referee.py` (tree-sitter) | `tree-sitter 0.25.2 / tree-sitter-<lang> …` |
| `ast-referee-tsc.js` | `typescript 5.2.2` |
| `ast-referee-cpython.py` | `cpython 3.13.9` |
| `ast-referee-roslyn` | `Microsoft.CodeAnalysis.CSharp 4.14.0.0 / dotnet 10.0.11` |
| `ast-referee-sqlref.py` | `pglast 8.4 / sqlglot 30.17.0 / sqlfluff 4.3.0 / scriptdom 17.0.0.0` |
| `ast-referee-ctags.py` | `universal-ctags 6.2.1` |
| `ast-referee-goast` | `go1.26.6` |
| `ast-referee-phpast.php` | `php 8.5.9 / ext-ast 1.1.3 / ast-node-version 110` |
| `ast-referee-ktpsi` | `kotlin-compiler 2.4.10 / jvm 25.0.2` |

`pip install -r requirements.txt` reproduces the Python side exactly, including Graphify.

## The corpora

Every repository is pinned to the commit measured — see `corpora/CORPORA.md`. 60 in Corpus A,
40 in Corpus B, 2 polyglot.

---

## Reproducibility, verified by running it

- **The referees are deterministic.** `go/parser` run twice over the same checkout produces a
  byte-identical truth file.
- **Graphify is deterministic in everything we score.** Two clean `graphify update --no-cluster`
  runs over the same repository at two different absolute paths produce identical `label`,
  `source_file`, `source_location`, `file_type` and `type` on all 876 nodes of cobra.
- **Its node `id`s are NOT stable across paths** — they embed the checkout directory name
  (`a_positionalargs` vs `b_positionalargs`). The scorer matches on `(file, name)` and never on
  `id`, so this does not move a number, but a byte-diff of two `graph.json` files will show it.
- **The score reproduces.** Both independent runs scored identically to each other *and* to the
  published cobra figures: methods 98.7% / 100.0% (588 matched of 588 found), types+constants
  89.5% / 94.4% (17 of 18).
- **Koragraph's extraction is deterministic**, differing between runs only in an `elapsed_ms`
  timing field.
- **The whole harness is deterministic across full runs.** Between two complete runs of all three
  corpora and all ten languages, **all 116 comparison artifacts were identical** in every scored
  quantity. That is the check a reader should repeat before trusting any change to this harness.
- **This folder reproduces the published result set on all three corpora.** It was set up in a
  clean environment strictly by its own README and run over every language of every corpus — 60
  Corpus A cells, 40 Corpus B, 16 polyglot. **All 116 matched exactly**: every Graphify recall,
  precision, matched/found count and method line accuracy, every truth set, and every
  `referee_version` stamp. The Corpus B and polyglot legs used a second, independently rebuilt
  copy rather than reusing the first one's environment.
