# Methodology notes and limits

What is known about the measurement itself: where it has to be pinned, how the published metrics
are defined, and which metric is withdrawn and why. Every magnitude here was produced by running
code.

---

## Determinism and the seed pin

Graphify's retrieval is non-deterministic between processes. Three identical `graphify query`
invocations — same graph, same question, same budget — returned different contexts with different
edge sets:

```
run 1  6525 chars
run 2  6619 chars
run 3  6549 chars
```

The cause is `PYTHONHASHSEED`. `serve.py#_bfs` returns a `set` of node ids and the rendered edge
list is built by iterating it, so Python's per-process string-hash randomisation changes which
edges render and therefore which survive the character-budget cut.

```
PYTHONHASHSEED=0   6573 / 6573 / 6573 chars, md5 identical
PYTHONHASHSEED=1   6619
PYTHONHASHSEED=7   6649
PYTHONHASHSEED=42  6549
```

`run-graphify.py` pins `PYTHONHASHSEED`, records it in every artifact, propagates it to CLI
subprocesses, and **refuses to start without it**. Recall itself is invariant across seeds
0/1/7/42/12345 (band **0.000**); only rendering order moves. Without the pin, Graphify's column
would carry unquantified run-to-run noise and could not be reproduced by a third party.

---

## Precision is withdrawn

Precision was defined as gold-hits / distinct-identifiers-named. It is confounded three ways, each
measured:

1. **The denominator counts format boilerplate.** 16 identifier-like tokens appear in ≥95% of
   Koragraph contexts (`knowledge`, `symbols`, `relevant`, `similarity`, `detail`, …) against 7
   for Graphify (`traversal`, `depth`, `node`, …). The metric partly measures each system's
   section headings.
2. **The denominators saturate differently.** Koragraph's retriever returns a fixed ~56-60 node
   candidate set regardless of budget, so its denominator plateaus at ~132 symbols while
   Graphify's keeps climbing to ~224 by budget 32000. Any Koragraph "precision lead" at high
   budget is largely Koragraph having nothing further to return.
3. **Its own stated validity condition is never met.** `score.py` defines precision as "a RATIO to
   compare across systems at equal recall". The systems are not at equal recall in any cell.

**Withdrawn.** The field is still emitted in `finalN.json` for inspection but is not a published
result and must not be quoted as one.

---

## Recall per 1k tokens is not published as a win

The token-normalised figure charges Graphify for boilerplate it is not permitted to count against
its own budget: its header, truncation notice and tail sit outside `budget × 3` by design but
inside the tokens the scorer measures — 117 tokens (13.7% of its scored tokens) at budget 1000, 91
(3.5%) at 4000. Re-scoring Graphify under Koragraph's strict cut changes its recall by **0.000** at
every budget, so the rule costs it no recall, but it does move the token-normalised figure.

---

## Graphify's traversal depth was pinned to 2, and depth is the single largest variable

`graphify query` hardcodes `depth=2` and exposes no flag (`cli.py:949`), so the original harness
used 2. But `_query_graph_text` itself defaults to **3** (`serve.py:956`), and Graphify's MCP tool
schema defaults to **3** with a documented maximum of **6** (`serve.py:1204`, `serve.py:1339`).
Benchmarking only the CLI's minimum understates Graphify on the surface that actually competes with
an MCP context server, and recall is monotone increasing in depth across the whole range.

`run-graphify.py` now takes `--depth`. Pooled over all 401 questions, all four repos:

| depth | recall @1000 | @4000 | @8000 | @32000 | tokens @32000 |
|---|---|---|---|---|---|
| 2 (CLI) | 0.4190 | 0.4985 | 0.5092 | 0.5180 | 7,303 |
| 3 (function + MCP default) | 0.4287 | 0.5449 | 0.5700 | 0.5923 | 13,265 |
| 4 | 0.4253 | 0.5731 | 0.6242 | 0.6677 | 17,629 |
| 6 (MCP documented max) | 0.4323 | 0.6144 | 0.6810 | **0.7689** | 20,414 |

**Depth 6 is the configuration Graphify must be compared against**, and it is what every published
figure here uses for the headline. At depth 2 the reported gap is roughly three times its true
size. CLI output verification is skipped at depth != 2, because the CLI cannot express it; the
depth is recorded in every artifact.

## The cpp cell is carried by preprocessor macros, and is reported separately

Plain C in Graphify's extractor models one declaration plane (`extract.py`, `_C_CONFIG`:
`function_types={"function_definition"}`, `class_types=frozenset()`, no `preproc_def`). 38 of
zlib's 204 gold declarations are macros. Splitting zlib's 120 questions by whether their gold
contains a macro, against Graphify at depth 6:

| budget | with macro gold (n=28) | without (n=92) |
|---|---|---|
| 4000 | K 0.610 / G 0.202 (**+0.408**) | K 0.586 / G 0.671 (**−0.085**) |
| 8000 | K 0.705 / G 0.229 (**+0.476**) | K 0.619 / G 0.748 (**−0.129**) |
| 32000 | K 0.887 / G 0.301 (**+0.586**) | K 0.860 / G 0.835 (+0.025) |

**On non-macro C questions Graphify at depth 6 beats Koragraph at budgets 4000 and 8000.** That is
a real loss and it is published as one. The cpp cell measures declaration-plane coverage at least
as much as retrieval, so it is reported split rather than pooled into a retrieval claim.

Twelve of the 38 macros are compile-time boilerplate no developer would search for (`NULL`,
`WIN32_LEAN_AND_MEAN`, `_CRT_SECURE_NO_WARNINGS`, the include guard `ZLIBIOAPI64_H`,
`_POSIX_C_SOURCE`). They are left in because the question set is generated mechanically and is not
edited in response to a score — but the split above is the number to read.

## Coverage versus retrieval reach

Rendering every node of each shipped `baselines/<repo>/graph.json` and scoring gold against it
gives Graphify's architectural ceiling: **0.894 pooled (0.865 symbol+file), 76 of 719 gold
declarations (10.6%) unreachable.** Against its published depth-2 recall of 0.518 that is 76
indexing misses versus 271 retrieval misses — **indexing is 21.9% of its failures.** Any claim that
the gap is mostly a modelling-coverage gap is false, and was corrected in `README.md`.

## Zero API cost is now evidenced rather than asserted

Every Koragraph artifact records `ingest_cost_ledger` — `koragraph.cost_ledger` grouped by provider
for that project. All seven benchmark projects return zero rows. The previous `llm_extraction`
field was read from the retrieval process's environment, which cannot observe how the graph was
built; it recorded `on` while the README said `off`. It has been removed rather than corrected,
because no field evaluated in that process can attest to ingest configuration.

## Retrieval configuration is now recorded per artifact

`run-koragraph.js` loads `koragraph_api/.env` with `override: true`, so local configuration wins
over the process environment. Arms previously carried no record of the resolved knobs, meaning two
arms could differ for reasons no artifact showed. Every artifact now records `retrieval_config`
(`RETRIEVAL_TOP_K`, `SUBGRAPH_MAX_NODES`, the ceilings, and the tokens-per-node constant).
