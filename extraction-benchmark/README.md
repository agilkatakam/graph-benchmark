# Declaration-extraction benchmark — Koragraph vs Graphify

Everything needed to reproduce the extraction-layer comparison in the paper: the referees, the
harness, the corpora (pinned to commit SHAs), and the results.

**Nothing here calls an LLM or the network at measurement time.** Both systems are run on their
zero-LLM path — Graphify `update --no-cluster`, Koragraph on its no-LLM extraction path (`LLM_EXTRACTION=off`), which for this benchmark
drives the AST extractor directly and issues no external model calls.

---

## What is being measured

For each of ten languages, both systems extract declarations from the same repositories. Both are
scored against an **independent third-party referee** — never against each other, and never
against either system's own definition of what a declaration is.

Four declaration planes: **types**, **methods**, **fields**, **constants**. Matching is a
`(file, name)` multiset, so an overload that one side collapses into a single node counts as a
recall miss rather than being forgiven. (Set-semantics figures are reported alongside — see
`results/`.)

---

## The referees

The rule: **prefer the language's own compiler front end**, and it is usable only if it can parse
ONE file with no build configuration, no classpath and no dependency resolution. That property is
what rules clang out for C/C++ and what rules `go/parser`, `ext/ast` and the Kotlin PSI in.

A tree-sitter referee cannot grade a tree-sitter extractor: it would only ever disagree about
taxonomy. Where both systems parse with tree-sitter, the primary referee is something else.

| language | primary referee | independent of Koragraph? | of Graphify? |
|---|---|---|---|
| Java | tree-sitter-java | yes (our Java path is regex) | no — same grammar |
| TypeScript | **TypeScript compiler** (`typescript` 5.x AST) | yes | yes |
| JavaScript | **TypeScript compiler** | yes | yes |
| Python | **CPython `ast`** | yes | yes |
| C# | **Roslyn** | yes | yes |
| SQL | **pglast / ScriptDom / sqlglot**, dialect-routed | yes | yes |
| C/C++ | **Universal Ctags** | partly — see below | partly |
| Go | **`go/parser`** | yes | yes |
| PHP | **`ext/ast`** (PHP's own AST) | yes | yes |
| Kotlin | **Kotlin compiler PSI** | yes | yes |

Every language additionally carries a **second referee** whose disagreement with the first is
reported rather than hidden.

**C/C++ is the weak referee and we say so.** Universal Ctags is a hand-written declaration
indexer, not a compiler front end — clang was rejected because it needs include paths and a
compilation database, i.e. a working build per repository. The second referee (tree-sitter) is
the grammar family both systems under test use. Neither is fully independent, the two disagree on
**18.9%** of the method plane, and swapping the referee moves Koragraph's figures by −3.5 to +1.6
points and Graphify's by −4.8 to +0.6 without changing who leads on any plane or either measure.
Both sets of numbers are published; regenerate with `harness/ast-second-referee.sh --lang cpp`.

---

## Symmetry rules

Every rule below is applied to **truth, to Koragraph, and to Graphify** — an exclusion applied to
only two of the three is a defect, and eight such defects were found and fixed during the audit
(see `results/AUDIT.md`).

- **Skip sets and file extensions** are identical on all three sides, per language.
- **Name normalisation** (SQL schema qualification, C++ `operator` spacing and template argument
  lists, Kotlin backticks) is applied on all three sides.
- **Unadjudicable regions** are excluded from all three sides: `#if`-disabled C# regions, `#if 0`
  C/C++ blocks, macro-generated C declarations, SQL routine bodies and statements no dialect
  parser can read.
- **Container nodes are not declarations** and are excluded from whichever system emits them:
  C# `namespace`, TypeScript `namespace`/`declare module`, SQL `ALTER` targets, Go receiver-owner
  stubs, C/C++ forward declarations.
- **Files one system cannot open** are excluded from both: Python `.pyi` stubs are not in
  Graphify's extension dispatch, so they are excluded from the head-to-head and reported
  separately.
- **`graphify-out/` must never sit inside a checkout.** Graphify's own AST cache, parsed as
  source, inflates *our* side by tens of thousands of nodes and reads as a win. The harness
  refuses to run if it finds one.

---

## Layout

```
referees/     one referee per language, each with its taxonomy fixed in its header comment
              package.json pins the TypeScript compiler, which IS the TS/JS referee
harness/      the runner, the scorer, the aggregator, the table generator, and
              ast-sensitivity.py — every scoring decision re-scored with that decision off
corpora/      CORPORA.md — every repository pinned to the exact commit measured
results/      AUDIT.md — every defect found against this benchmark, with its measured size
```

**What is NOT here: Koragraph's extractor.** It is not redistributed, so the harness feeds the
scorer an empty node set for our side and **every Koragraph column reads 0.00%**. That is an
absent input, not a result. What this folder reproduces is **Graphify's side** — the same
referee, the same scorer, the same corpora, the same competitor — which is the half that lets a
third party check us. Every Graphify figure in the paper is reproducible from here alone.

---

## Reproducing

Read `VERSIONS.md` first. Every referee stamps `referee_version` into its truth file, so a
toolchain mismatch is visible rather than silent — but the pins are not optional if you want the
published digits.

```bash
# 1. Python side, in a virtualenv. `python3` in the harness must be THIS interpreter.
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# 2. TypeScript/JavaScript referee: the compiler version decides truth, so it is pinned.
(cd referees && npm ci)

# 3. Compiled referees. Each is shipped as source and built in place.
(cd referees/ast-referee-goast     && go build -o referee .)
(cd referees/ast-referee-roslyn    && dotnet build -c Release)
(cd referees/ast-referee-scriptdom && dotnet build -c Release)
(cd referees/ast-referee-ktpsi     && kotlinc -cp "$(brew --prefix)/opt/kotlin/libexec/lib/kotlin-compiler.jar" \
   -opt-in=org.jetbrains.kotlin.config.CompilerConfiguration.Internals \
   -opt-in=org.jetbrains.kotlin.K1Deprecation Referee.kt -include-runtime -d referee.jar)
# C/C++ needs Universal Ctags 6.2.1 on PATH; PHP needs php + ext/ast. See VERSIONS.md.

# 4. Clone the corpora at the pinned SHAs (see corpora/CORPORA.md) into ./checkouts/

# 5. Build Graphify baselines. This relocates graphify-out/ OUT of the checkout, which matters:
#    left inside, Graphify's own AST cache is parsed as source on the next run.
AST_CHECKOUT_DIR=$PWD/checkouts AST_BASELINE_DIR=$PWD/baselines ./harness/graphify-baseline.sh cobra

# 6. Score.
AST_CHECKOUT_DIR=$PWD/checkouts AST_BASELINE_DIR=$PWD/baselines ./harness/ast-bench.sh --lang go cobra
python3 harness/ast-aggregate.py --lang go
```

Verified end to end from a directory outside the Koragraph repository: cobra scores Graphify at
**98.7% / 100.0% on methods (588 matched of 588 found)** and **89.5% / 94.4% on types+constants
(17 of 18)**, identical to the private harness and to the published figures.

`AST_CHECKOUT_DIR` and `AST_BASELINE_DIR` point the same harness at a different corpus; that is
how the held-out run is produced, so development and held-out results cannot come from different
code.

---

## Development and held-out corpora

> **Artifact naming.** In this release the development corpus ships as
> `results/artifacts/ast-bench/` and the **held-out** corpus as
> `results/artifacts/ast-bench-corpus-b/`. An earlier release called the latter only "Corpus B",
> which stated nothing false but removed the one fact a reviewer needs to judge overfitting —
> that those 40 repositories were chosen and cloned *after* development finished. It is named as
> held-out here and everywhere below.

Koragraph was iterated against the **development** corpus; Graphify was run once, unmodified, at
v0.9.28. That asymmetry is ordinary product development, not benchmark tuning — but it is exactly
what a held-out corpus tests. Four further repositories per language, chosen and cloned after the
development work was complete, are measured with the same harness and reported alongside.

Where a fix failed to transfer, the gap is reported and the cause named. Two of them were real
generalisation bugs found this way and fixed:

- C-family block comments do not nest — a scanner that treated `/*` inside a comment as a nested
  opener never closed jackson-databind's section banners, and the enclosing class lost every
  field.
- A wrapped class header puts the type's own opening brace on a line of its own, which the
  initializer-block scan read as `static { ... }` and treated the whole class body as executable.

Neither was visible on any of the seven development Java repositories.
