#!/usr/bin/env bash
# Re-score a language against its SECOND referee, and report how far the two referees disagree.
#
# Every language in this benchmark carries two referees: the primary, and a second independent
# implementation of the same taxonomy. For C/C++ the primary is Universal Ctags and the second is
# the tree-sitter grammar family both systems under test parse with — which is why it cannot be
# the primary, and why the spread between the two referees is reported rather than one of them.
#
# The extractor dumps are reused from the primary run — the system under test does not change,
# only the referee does — so this must run AFTER ast-bench.sh for the same language.
#
#   ./harness/ast-second-referee.sh --lang cpp
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=python3
PRIMARY_WORK="${AST_BENCH_WORK:-${TMPDIR:-/tmp}/ast-bench}"
WORK="${AST_SECOND_WORK:-${PRIMARY_WORK}-second}"
CHECKOUT_DIR="${AST_CHECKOUT_DIR:-$ROOT/.corpus-a-checkouts}"
BASELINE_DIR="${AST_BASELINE_DIR:-$ROOT/.corpus-a-baselines}"
mkdir -p "$WORK"

LANG_ARG=cpp
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --lang) LANG_ARG="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

REPOS=("${ARGS[@]:-}")
if [ ${#ARGS[@]} -eq 0 ]; then
  REPOS=(redis leveldb libuv googletest nlohmann-json curl)
fi

for r in "${REPOS[@]}"; do
  CHECKOUT="$CHECKOUT_DIR/$r"
  KORA="$PRIMARY_WORK/kora_${LANG_ARG}__${r}.json"
  [ -d "$CHECKOUT" ] || { echo "missing checkout: $CHECKOUT" >&2; exit 1; }
  # Reusing the primary run's extractor dump is what makes this a referee comparison rather than
  # a second measurement of two things at once. If it is absent the primary run has not been
  # made, and silently re-dumping would hide that.
  [ -f "$KORA" ] || { echo "missing primary-run dump: $KORA — run ast-bench.sh --lang $LANG_ARG first" >&2; exit 1; }

  echo "===== $r ====="
  # Go, PHP and Kotlin have a compiler front end as primary and ctags as second; the other seven
  # have ctags or a compiler as primary and the tree-sitter grammar as second.
  case "$LANG_ARG" in
    go|php|kotlin)
      "$PY" "$ROOT/referees/ast-referee-ctags.py" "$CHECKOUT" \
        --lang "$LANG_ARG" --out "$WORK/truth_${LANG_ARG}__${r}.json" >/dev/null ;;
    *)
      "$PY" "$ROOT/referees/ast-referee.py" "$CHECKOUT" \
        --lang "$LANG_ARG" --out "$WORK/truth_${LANG_ARG}__${r}.json" >/dev/null ;;
  esac

  case "$LANG_ARG" in
    go|php|kotlin) SECOND_REFEREE="Universal Ctags" ;;
    *)             SECOND_REFEREE="tree-sitter-$LANG_ARG" ;;
  esac

  ARGS_CMP=(--truth "$WORK/truth_${LANG_ARG}__${r}.json" --kora "$KORA" --lang "$LANG_ARG"
            --repo-name "$r" --checkout "$CHECKOUT" --referee "$SECOND_REFEREE")
  [ -f "$BASELINE_DIR/$r/graph.json" ] && ARGS_CMP+=(--graphify "$BASELINE_DIR/$r/graph.json")
  "$PY" "$ROOT/harness/ast-layer-compare.py" "${ARGS_CMP[@]}" \
    > "$WORK/cmp_${LANG_ARG}__${r}.json"
done

echo
"$PY" "$ROOT/harness/ast-paper-tables.py" --referees \
  --lang "$LANG_ARG" --work "$PRIMARY_WORK" --second-work "$WORK" \
  --repos "$(IFS=,; echo "${REPOS[*]}")"
