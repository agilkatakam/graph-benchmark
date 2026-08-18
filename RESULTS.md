# Koragraph vs Graphify — results

Every figure below regenerates from the artifacts in this repository. Where a number moved since
the previous release, the previous number and the reason are stated rather than quietly replaced.

---

## 1. Extraction

10 languages, 100 repositories, 1,089,175 head-to-head declarations, refereed by each language's
own compiler front end (`go/parser`, CPython `ast`, Roslyn, `tsc`, PHP `ext/ast`, Kotlin PSI) and
Universal Ctags for C/C++. Never our parser, never theirs.

| | Koragraph | Graphify |
|---|---|---|
| Declaration recall | **99.70%** | 82.07% |
| Precision | 99.78% | **99.86%** |

Graphify wins precision. **The difference that survives is coverage, not correctness** — Graphify
models no field plane in 7 of 10 languages, a plane of 461,804 declarations where Koragraph reaches
99.53% recall at 99.57% precision. Across all 116 repository cells Koragraph leads 114 and ties 2
(`go` in `protobuf`, 8 declarations; `sql` in `northwind-psql`, 14); Graphify leads none.

Polyglot corpus (`thrift`, `protobuf`): 111,541 declarations, 96.51% vs 63.39%, Koragraph leading
15 of 16 language-repository rows and tying the sixteenth.

The development corpus (60 repos) and the **held-out** corpus (40 repos, cloned after development
finished) are reported separately — see `extraction-benchmark/README.md`. The full adversarial
record, including 13 defects found in our own harness that each made Graphify look worse than it
was, is in `extraction-benchmark/results/AUDIT.md`.

---

## 2. Retrieval

401 questions, 4 repositories, budgets 1000–32000. Questions are maintainers' own commit subjects;
gold answers are located by Universal Ctags 6.2.1, a third-party indexer neither system uses. The
scorer is blind — it receives `(context, gold)` with no system identity.

**Graphify is compared at traversal depth 6, its MCP-documented maximum.** Its CLI hardcodes depth
2 (`cli.py:949`), but `_query_graph_text` defaults to 3 and the MCP tool schema defaults to 3 with
a documented max of 6. The previous release published the depth-2 figure only, which overstated the
gap by roughly 3×. Both arms ship here: `artifacts/g6-d6/` (headline) and `artifacts/g6/` (depth 2).

### Pooled, paired by question, n=401

| budget | Koragraph | Graphify d6 | paired difference, 95% CI |
|---|---|---|---|
| 1000 | 0.521 | 0.432 | +0.089 [+0.044, +0.135] |
| 2000 | 0.543 | 0.528 | +0.015 [−0.031, +0.063] — **tie** |
| 4000 | 0.649 | 0.614 | +0.035 [−0.016, +0.085] — **tie** |
| 8000 | 0.732 | 0.681 | +0.051 [+0.001, +0.100] |
| 16000 | 0.824 | 0.728 | +0.096 [+0.048, +0.144] |
| 32000 | 0.891 | 0.769 | +0.122 [+0.077, +0.167] |

Graphify's best result at any depth or budget is **0.769 using 20,414 measured tokens**. Koragraph
reaches **0.824 using 11,343** — +0.055 [+0.006, +0.103] on 45% fewer tokens.

### Per repository — recall / symbol+file attribution

| language | repo | n | budget | Koragraph | Graphify d6 |
|---|---|---|---|---|---|
| cpp | `zlib` | 120 | 1000 | 0.488 / 0.466 | 0.371 / 0.264 |
| cpp | `zlib` | 120 | 8000 | 0.639 / 0.598 | 0.627 / 0.502 |
| cpp | `zlib` | 120 | 32000 | 0.866 / 0.816 | 0.710 / 0.610 |
| go | `viper` | 120 | 1000 | 0.597 / 0.593 | 0.544 / 0.538 |
| go | `viper` | 120 | 8000 | 0.844 / 0.820 | 0.809 / 0.803 |
| go | `viper` | 120 | 32000 | 0.983 / 0.968 | 0.809 / 0.803 |
| java | `commons-cli` | 120 | 1000 | 0.463 / 0.447 | 0.363 / 0.302 |
| java | `commons-cli` | 120 | 8000 | 0.697 / 0.642 | 0.580 / 0.563 |
| java | `commons-cli` | 120 | 32000 | 0.832 / 0.792 | 0.773 / 0.763 |
| python | `requests` | 41 | 1000 | 0.562 / 0.528 | 0.487 / 0.414 |
| python | `requests` | 41 | 8000 | 0.774 / 0.716 | 0.760 / **0.724** |
| python | `requests` | 41 | 32000 | 0.868 / 0.868 | 0.809 / 0.772 |

---

## 3. Where Graphify wins, and what this does not show

- **Pooled extraction precision** (99.86% vs 99.78%) and **line accuracy in 7 of 20 cells**.
  Graphify's precision is 93–100% on every plane of every language. Its extraction is about as
  correct as ours.
- **Budgets 2000 and 4000 are ties.** The confidence interval crosses zero. They are not wins.
- **Attribution at `requests` budget 8000** (0.724 vs 0.716).
- **Non-macro C.** 38 of zlib's 204 gold declarations are preprocessor macros, which plain C in
  Graphify's extractor models none of. Split zlib's questions by whether their gold contains a
  macro: with macros Koragraph leads by +0.41 to +0.59; **without them Graphify at depth 6 beats
  Koragraph at budget 4000 (−0.085) and 8000 (−0.129).** The cpp cell measures declaration-plane
  coverage at least as much as retrieval and is reported split for that reason.

**How much of the gap is coverage.** Rendering every node of each shipped `baselines/<repo>/
graph.json` and scoring gold against it gives Graphify's architectural ceiling: **0.894 pooled, 76
of 719 gold declarations (10.6%) unreachable.** Against its depth-2 recall of 0.518 that is 76
indexing misses versus 271 retrieval misses — **indexing is 21.9% of its failures.** A previous
release named declaration-plane coverage as the main cause of the gap. That was wrong, and this
repository's own shipped data is what disproves it.

**Precision** as previously defined is withdrawn as confounded — see `retrieval-benchmark/results/
AUDIT.md`.

---

## 4. Scope

Four single-language repositories of 20–63 source files; 401 questions. Not a general claim about
either product on repositories of arbitrary size. Cross-repository linking is not measured. Both
systems run at zero API cost: Graphify with `--no-cluster`, Koragraph on its no-LLM ingest path —
every Koragraph artifact records `ingest_cost_ledger`, the project's cost ledger grouped by
provider, and all benchmark projects return zero rows.

**Graphify's column is reproducible from this repository at every depth from 2 to 6. Koragraph is
closed source, so its column can be re-scored from the shipped contexts but not re-run.** Stated
plainly rather than implied to be symmetric.
