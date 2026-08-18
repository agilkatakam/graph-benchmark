#!/usr/bin/env bash
# One-command AST-layer benchmark: Koragraph vs Graphify, both scored against tree-sitter
# ground truth (the grammar Graphify itself parses with).
#
# Zero DB, zero LLM, zero network, no services required.
#
#   ./harness/ast-bench.sh                      # Java corpora (default)
#   ./harness/ast-bench.sh --lang typescript    # TypeScript corpora
#   ./harness/ast-bench.sh --lang typescript zod nest
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${AST_BENCH_WORK:-${TMPDIR:-/tmp}/ast-bench}"
mkdir -p "$WORK"

# Artifacts are keyed by LANGUAGE and repo, not by repo alone: on a polyglot checkout, running a
# second language would otherwise overwrite the first one's truth and comparison files.
key() { echo "${1}__${2}"; }

LANG_ARG=java
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --lang) LANG_ARG="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

REPOS=("${ARGS[@]:-}")
if [ ${#ARGS[@]} -eq 0 ]; then
  if [ "$LANG_ARG" = "typescript" ]; then
    REPOS=(zod nest angular excalidraw vue-core)
  elif [ "$LANG_ARG" = "csharp" ]; then
    REPOS=(sharex newtonsoft-json jellyfin efcore avalonia abp)
  elif [ "$LANG_ARG" = "python" ]; then
    REPOS=(django-machina fastapi flask poetry django pandas)
  elif [ "$LANG_ARG" = "javascript" ]; then
    REPOS=(express axios lodash eslint webpack gatsby)
  elif [ "$LANG_ARG" = "sql" ]; then
    REPOS=(chinook-database frk test-db mssql-maintenance timescaledb citus)
  elif [ "$LANG_ARG" = "cpp" ]; then
    REPOS=(redis leveldb libuv googletest nlohmann-json curl)
  elif [ "$LANG_ARG" = "go" ]; then
    REPOS=(gin cobra grpc-go etcd hugo prometheus)
  elif [ "$LANG_ARG" = "php" ]; then
    REPOS=(monolog slim guzzle composer framework phpunit)
  elif [ "$LANG_ARG" = "kotlin" ]; then
    REPOS=(mockk arrow coroutines detekt exposed ktor)
  else
    REPOS=(spring-petclinic-rest gson spring-petclinic commons-lang netty mall dubbo)
  fi
fi

for r in "${REPOS[@]}"; do
  CHECKOUT="${AST_CHECKOUT_DIR:-$ROOT/.corpus-a-checkouts}/$r"
  BASELINE="${AST_BASELINE_DIR:-$ROOT/.corpus-a-baselines}/$r/graph.json"
  [ -d "$CHECKOUT" ] || { echo "missing checkout: $CHECKOUT" >&2; exit 1; }

  # Graphify's own output must never sit inside a tree that is parsed or ingested, or the
  # baseline contaminates the corpus.
  if find "$CHECKOUT" -name graphify-out -maxdepth 3 | grep -q .; then
    echo "REFUSING: graphify-out is inside $CHECKOUT — move it to .corpus-a-baselines/" >&2
    exit 1
  fi

  ROSLYN="$ROOT/referees/ast-referee-roslyn/bin/Release/net10.0/referee.dll"
  # TypeScript and JavaScript ground truth is the TypeScript compiler's own AST. Both systems
  # under test parse with tree-sitter, and `ast-referee.py`'s TS/JS collectors share node-type
  # sets with one of them, so it cannot be the primary referee here. `tsc` is the definition of
  # a TypeScript declaration and neither system uses it. Same choice as Roslyn for C#, go/parser
  # for Go, ext/ast for PHP, PSI for Kotlin and CPython's `ast` for Python.
  # `ast-referee.py --lang typescript|javascript` is the second referee.
  TSCREF="$ROOT/referees/ast-referee-tsc.js"
  if [ "$LANG_ARG" = "typescript" ] || [ "$LANG_ARG" = "javascript" ]; then
    node "$TSCREF" "$CHECKOUT" --lang "$LANG_ARG" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  # C# ground truth comes from Roslyn when it is built. The tree-sitter C# grammar is not only
  # non-independent here, it errors on a substantial fraction of large files and under-reports
  # their declarations, which scores correct extractions as false positives.
  elif [ "$LANG_ARG" = "csharp" ] && [ -f "$ROSLYN" ] && command -v dotnet >/dev/null 2>&1; then
    dotnet "$ROSLYN" "$CHECKOUT" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  # SQL ground truth is dialect-routed to a production parser per dialect. The tree-sitter SQL
  # grammar — which is what Graphify parses with — errors over 96.5%-100% of the bytes of every
  # repo here and finds zero of the 49 stored procedures in the two T-SQL corpora, so it is
  # kept as the competitor's-grammar reference (ast-referee.py --lang sql) and never as truth.
  elif [ "$LANG_ARG" = "sql" ]; then
    # stdout is the referee's summary and is noise here; stderr is deliberately NOT suppressed —
    # a missing parser dependency reports itself there, and swallowing it turns an absent truth
    # set into what looks like a 0.0% result.
    python3 "$ROOT/referees/ast-referee-sqlref.py" \
      "$CHECKOUT" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  # C/C++ ground truth is Universal Ctags. Both systems under test parse with tree-sitter, so a
  # tree-sitter referee grades two systems sharing a grammar — and it errors on 26%-63% of this
  # corpus's bytes. ast-referee.py --lang cpp is kept as the second referee.
  elif [ "$LANG_ARG" = "cpp" ]; then
    python3 "$ROOT/referees/ast-referee-ctags.py" \
      "$CHECKOUT" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  # Go ground truth is go/parser — the compiler's own front end. Both systems under test parse
  # Go with tree-sitter, so a tree-sitter referee would grade two systems sharing a grammar.
  # Unlike clang for C++, go/parser needs no build configuration and ignores build constraints,
  # so it reads every file exactly as written. ast-referee-ctags.py --lang go is the second.
  elif [ "$LANG_ARG" = "go" ]; then
    GOAST="$ROOT/referees/ast-referee-goast/referee"
    [ -x "$GOAST" ] || { echo "build it: (cd referees/ast-referee-goast && go build -o referee .)" >&2; exit 1; }
    "$GOAST" "$CHECKOUT" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  # PHP ground truth is the PHP compiler's own AST, via ext/ast. Not a reimplementation of PHP's
  # grammar — it IS PHP's grammar, and it needs no autoloader, composer install or include path.
  # ast-referee-ctags.py --lang php is the second referee (two blind spots, see its header).
  elif [ "$LANG_ARG" = "php" ]; then
    command -v php >/dev/null 2>&1 || { echo "php not found: brew install php && pecl install ast" >&2; exit 1; }
    php "$ROOT/referees/ast-referee-phpast.php" "$CHECKOUT" \
      --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  # Kotlin ground truth is the Kotlin compiler's own PSI. Both systems under test parse Kotlin
  # with tree-sitter, so a tree-sitter referee would grade two systems sharing a grammar.
  # Building a KtFile needs no classpath and no build configuration — the property that made
  # go/parser and PHP's ext/ast usable. ast-referee-ctags.py --lang kotlin is the second.
  #
  # Python ground truth is CPython's own `ast` module — the compiler's front end, the same
  # choice made for Go, PHP, C# and Kotlin. Koragraph's Python plane is tree-sitter and so is
  # Graphify's, so a tree-sitter referee would grade two systems that share a grammar and could
  # only ever disagree about taxonomy. ast-referee.py --lang python is the second referee.
  elif [ "$LANG_ARG" = "python" ]; then
    python3 "$ROOT/referees/ast-referee-cpython.py" \
      "$CHECKOUT" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  elif [ "$LANG_ARG" = "kotlin" ]; then
    KTJAR="$ROOT/referees/ast-referee-ktpsi/referee.jar"
    KTC="$(brew --prefix 2>/dev/null || echo /opt/homebrew)/opt/kotlin/libexec/lib/kotlin-compiler.jar"
    [ -f "$KTJAR" ] || { echo "build it: (cd referees/ast-referee-ktpsi && kotlinc -cp $KTC -opt-in=org.jetbrains.kotlin.config.CompilerConfiguration.Internals -opt-in=org.jetbrains.kotlin.K1Deprecation Referee.kt -include-runtime -d referee.jar)" >&2; exit 1; }
    java -cp "$KTJAR:$KTC" RefereeKt "$CHECKOUT" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  else
  python3 "$ROOT/referees/ast-referee.py" \
    "$CHECKOUT" --lang "$LANG_ARG" --out "$WORK/truth_$(key "$LANG_ARG" "$r").json" >/dev/null
  fi
  # Koragraph is proprietary and is not redistributed, so this harness produces
  # only GRAPHIFY's side. An empty node set makes every Koragraph column read
  # 0.00%; that is not a result, it is an absent input. Read the GFY columns only.
  echo '{"nodes":[],"planes":{},"elapsed_ms":null}' > "$WORK/kora_$(key "$LANG_ARG" "$r").json"

  echo "===== $r ====="
  # bash 3.2 (macOS default) errors on empty-array expansion under `set -u`, so branch
  # on the baseline instead of splatting a possibly-empty argument array.
  if [ -f "$BASELINE" ]; then
    python3 "$ROOT/harness/ast-layer-compare.py" \
      --truth "$WORK/truth_$(key "$LANG_ARG" "$r").json" --kora "$WORK/kora_$(key "$LANG_ARG" "$r").json" --lang "$LANG_ARG" \
      --graphify "$BASELINE" --repo-name "$r" --checkout "$CHECKOUT" > "$WORK/cmp_$(key "$LANG_ARG" "$r").json"
  else
    python3 "$ROOT/harness/ast-layer-compare.py" \
      --truth "$WORK/truth_$(key "$LANG_ARG" "$r").json" --kora "$WORK/kora_$(key "$LANG_ARG" "$r").json" --lang "$LANG_ARG" \
      --repo-name "$r" --checkout "$CHECKOUT" > "$WORK/cmp_$(key "$LANG_ARG" "$r").json"
  fi
  python3 - "$WORK/cmp_$(key "$LANG_ARG" "$r").json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
has_gfy = d.get("graphify_baseline")
print(f"  truth: {d['truth_counts']}")
for e in d["entities"]:
    k, g = e["kora"], e["graphify"]
    # An entity the repo simply does not contain prints as 0.0% / 0.0% with miss=0 and
    # extra=0 — a vacuous row rather than a failure, so it is skipped.
    if not e['truth'] and not e['kora']['found']:
        continue
    if e['entity'] == 'types+constants' and not has_gfy:
        continue
    line = (f"  {e['entity']:<15} truth={e['truth']:<6}"
            f" | KORA rec={k['recall']:>5}% prec={k['precision']:>5}% miss={k['missed']:<5} extra={k['extra']:<5}")
    if has_gfy and e.get("graphify_scored", True):
        win = "KORA" if k["recall"] > g["recall"] else ("GRAPHIFY" if g["recall"] > k["recall"] else "tie")
        line += (f" | GFY rec={g['recall']:>5}% prec={g['precision']:>5}%"
                 f" miss={g['missed']:<5} extra={g['extra']:<5} -> {win}")
    elif has_gfy:
        # Not a zero — Graphify is not scored on this row at all. Its declarations are split
        # across `methods` and `types+constants` by its label convention and nothing finer
        # exists, so printing 0.0% here would understate it.
        line += " | GFY  not scored on this row (see types+constants)"
    else:
        line += " | (no Graphify baseline — referee-only)"
    print(line)
for kr in d.get("kora_recall_by_kind", []):
    print(f"      {kr['plane']:<7} {kr['kind']:<20} truth={kr['truth']:<6} kora_rec={kr['kora_recall']}%")
cp = d.get("comment_planes") or {}
if cp.get("kora_rationale") or cp.get("graphify_rationale"):
    print(f"  comments rationale K={cp['kora_rationale']} G={cp['graphify_rationale']}"
          f" | doc_ref K={cp['kora_doc_ref']} G={cp['graphify_doc_ref']}")
if d.get("extractor_planes"):
    print(f"  planes   {d['extractor_planes']}")
if has_gfy:
    print(f"  gfy-fields {d.get('graphify_nodes_landing_on_a_truth_field')} of its nodes land on a truth field")
print(f"  line-acc {d['method_line_accuracy_within_2']}")
print(f"  spans    {d['span_end_line_coverage']}")
print(f"  calls    {d['call_sites']}")
print(f"  extract  {d['extract_ms']['kora']} ms")
PY
done
