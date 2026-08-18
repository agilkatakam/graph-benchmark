# Koragraph vs Graphify — retrieval benchmark

Everything needed to reproduce **Graphify's column** to the digit. Koragraph is closed source, so
its column cannot be re-run from here — but its inputs, its outputs, the scorer and the questions
are all published, so every Koragraph number can be re-scored and checked against the artifacts.

Verified from a clean venv outside this repository: **2406/2406 Graphify context cells
byte-identical**, and all scored metric fields exact.

---

## What is measured

Both systems ingest the same repository and are asked the same questions at the same context
budgets. A question is a maintainer's own commit subject; the gold answer is the set of
declarations that commit modified, located by Universal Ctags — a third-party indexer neither
system uses.

| metric | definition |
|---|---|
| `recall` | fraction of gold declarations named anywhere in the returned context |
| `recall_file` | named **and** attributed to the correct file on the same line |
| `recall per 1k tokens` | recall / (measured tokens / 1000), tiktoken `cl100k_base` |
| `precision` | ⚠ **withdrawn as confounded — see `results/AUDIT.md`.** Still emitted in `finalN.json`, not a published result. |

Budgets: **1000, 2000, 4000, 8000, 16000, 32000**. Both sides are cut by Graphify's own
`budget × 3` character rule (`serve.py:802`).

## Corpus — pinned

| language | repo | commit | source files | LOC |
|---|---|---|---|---|
| python | `requests` | `8068356288978c4f54661ae6f95afe0e0831885e` | 20 | 6,403 |
| go | `viper` | `528f7416c4b56a4948673984b190bf8713f0c3c4` | 21 | 3,513 |
| cpp | `zlib` | `e3dc0a85b7032e98380dec011bc8f2c2ee0d8fca` | 63 | 36,170 |
| java | `commons-cli` | `1aaa321944c398da78b30c8e42e10a299a131f64` | 36 | 9,787 |

**Counting rule, stated because an earlier revision of this table was wrong.** "Source files" is
tracked files matching the language's extensions (`.py` / `.go` / `.c,.h,.cpp,.cc,.hpp` / `.java`)
after `build-questions.py`'s own `NON_SOURCE_RE`, and LOC is a raw line count of those files —
regenerate with the recount block in `harness/`. Before the whole checkout: 37 / 33 / 79 / 87 files
and 12,032 / 7,194 / 42,969 / 20,335 lines. The previous table gave zlib as 37 files / 23,301 lines,
which silently excluded `contrib/` — **17 of the 36 distinct zlib gold files live under `contrib/`**,
so that figure described a corpus narrower than the one the questions are drawn from. `commons-cli`
was and remains exact; `requests` and `viper` moved by one file.

The shipped baselines were built from checkouts at `/tmp/gb/<repo>`:

```bash
mkdir -p /tmp/gb && cd /tmp/gb
git clone https://github.com/psf/requests.git         && git -C requests     checkout 8068356288978c4f54661ae6f95afe0e0831885e
git clone https://github.com/spf13/viper.git          && git -C viper        checkout 528f7416c4b56a4948673984b190bf8713f0c3c4
git clone https://github.com/madler/zlib.git          && git -C zlib         checkout e3dc0a85b7032e98380dec011bc8f2c2ee0d8fca
git clone https://github.com/apache/commons-cli.git   && git -C commons-cli  checkout 1aaa321944c398da78b30c8e42e10a299a131f64
```

⚠ **`graph.json` is the authoritative input, not the checkout.** Graphify slugifies the absolute
checkout path into the ids of file nodes it does not otherwise materialise, so the same repository
built at a different path yields different node ids; building from a *relative* path additionally
rewrites `source_file` (measured: 505 of 549 viper nodes change), which silently changes
`recall_file`. Use the shipped `baselines/<repo>/graph.json`. If you rebuild, use an absolute path,
and use `/tmp/gb/<repo>` to reproduce the shipped ids exactly:

```bash
graphify update /tmp/gb/<repo> --no-cluster    # writes /tmp/gb/<repo>/graphify-out/graph.json
```

## Reproduce Graphify's column

```bash
python3 -m venv .venv-bench
.venv-bench/bin/pip install -r requirements.txt

mkdir -p /tmp/gbench-out
PYTHONHASHSEED=0 .venv-bench/bin/python3 harness/run-graphify.py \
  --questions questions/v3/go__viper.json \
  --graph baselines/viper/graph.json \
  --out /tmp/gbench-out/go__viper.json --verify-cli 5

.venv-bench/bin/python3 harness/score.py \
  --questions-dir questions/v3 \
  --systems graphify=/tmp/gbench-out \
  --out /tmp/my-scores.json
```

⚠ **The `--out` filename must match the questions filename.** `score.py` resolves an artifact as
`<systems-dir>/<language>__<repo>.json`, so writing to `/tmp/my-viper.json` and passing
`graphify=/tmp` skips every cell and exits `FATAL: no records to score`. An earlier revision of
this README gave exactly that command; it was verified to fail and is corrected above.

Add `--depth 6` to compare against Graphify's MCP-documented maximum rather than the depth of 2 its
CLI hardcodes — see `results/AUDIT.md`. Depth is the largest single variable in this benchmark.

`PYTHONHASHSEED` is **mandatory** and the script refuses to start without it. Graphify's `_bfs()`
returns a `set`, so Python's per-process string-hash randomisation changes which edges render and
therefore which survive the budget cut — measured at 6525 / 6619 / 6549 characters for one
identical query across three unpinned runs. Pinned, it is byte-identical. Recall itself is
invariant across seeds 0/1/7/42/12345 (band **0.000**); only rendering order moves.

`--verify-cli N` re-runs N questions through the real `graphify` binary and requires identical
output, so the in-process call cannot drift from what the CLI does.

## Layout

```
harness/          run-graphify.py, run-koragraph.js, score.py, build-questions.py,
                  pooled-stats.py
questions/v3/     401 questions, 4 languages — the published set
baselines/        Graphify graph.json per repo (authoritative input)
artifacts/g6-d6/  Graphify contexts at depth 6 (headline)
artifacts/g6/     Graphify contexts at depth 2 (what its CLI hardcodes)
artifacts/k11/    Koragraph contexts
results/          finalN.json (scored), AUDIT.md (methodology notes and limits)
```

## Requirements

`python3` (one interpreter runs both stages — see `requirements.txt`), and **Universal Ctags
6.2.x** only if you regenerate questions. BSD ctags emits nothing and exits 0; the generator
refuses it, and warns on a different Universal Ctags minor because gold sets differ between
releases.

## Scope

Four single-language OSS repositories of 20–37 source files, 401 questions, budgets 1000–32000.
The results are scoped to that corpus; they are not a general claim about either product on
repositories of arbitrary size.

This measures the **end-to-end path** each product ships: ingest plus retrieval.

**How much of the gap is coverage and how much is retrieval — measured, not asserted.** An earlier
revision of this README attributed Koragraph's lead largely to modelling declaration planes Graphify
does not (C macros, fields, constants). That is measurably wrong as a primary explanation. Rendering
every node of each shipped `baselines/<repo>/graph.json` and scoring the gold against it gives
Graphify's *architectural ceiling* — what it could return if its retrieval were perfect:

| repo | gold | in-graph ceiling | unreachable |
|---|---|---|---|
| requests | 67 | 0.806 | 13 |
| viper | 216 | 0.935 | 14 |
| zlib | 204 | 0.809 | 39 |
| commons-cli | 232 | 0.957 | 10 |
| **pooled** | **719** | **0.894** | **76 (10.6%)** |

Against a published Graphify recall of 0.518, that splits its 719 gold misses into **76 indexing
misses and 271 retrieval misses — indexing is 21.9% of its failures, not the bulk of them.** The
honest statement is that coverage explains roughly a fifth of the gap and retrieval reach explains
the rest. The one place coverage does dominate is C: 38 of zlib's 204 gold declarations are
preprocessor macros, which plain C in Graphify's extractor models zero of — see `results/AUDIT.md`
for why the cpp cell is reported separately.

Both systems run at **zero API cost**: Graphify with `--no-cluster` (its LLM clustering off),
Koragraph on its no-LLM ingest path. That is evidenced rather than asserted: every
Koragraph artifact records `ingest_cost_ledger` — `koragraph.cost_ledger` grouped by provider for
that project — and all benchmark projects return zero rows. A previous release instead carried an
`llm_extraction` field read from the *retrieval* process's environment, which cannot observe how
the graph was built; it recorded `on` while the prose said `off`. The field is removed. Koragraph additionally runs two local neural models — a
0.5B method-purpose model and BGE-M3 embeddings — on the user's own machine. Graphify's compared
path runs none.

`results/AUDIT.md` records the methodology limits that bear on these numbers, each with its
measured size — the seed pin, why precision is withdrawn, and why the token-normalised figure
is not published as a win.
