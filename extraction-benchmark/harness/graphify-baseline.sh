#!/usr/bin/env bash
# Build a Graphify baseline for a checked-out repo, with zero LLM, and relocate the output
# OUT of the checkout.
#
# `graphify update` has no --out flag: it always writes <repo>/graphify-out/. Left in place, that
# AST cache sits inside the corpus and is picked up as source by anything that walks the checkout
# afterwards. This script always relocates it and verifies the checkout is clean before exiting.
#
#   ./harness/graphify-baseline.sh <repo> [repo ...]
#
# AST_CHECKOUT_DIR / AST_BASELINE_DIR point the same harness at a different corpus. The
# corpus B uses .corpus-b-checkouts / .corpus-b-baselines so that no two corpora can be scored
# by different code.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRAPHIFY=graphify
CHECKOUT_DIR="${AST_CHECKOUT_DIR:-$ROOT/.corpus-a-checkouts}"
BASELINE_DIR="${AST_BASELINE_DIR:-$ROOT/.corpus-a-baselines}"

for r in "$@"; do
  CHECKOUT="$CHECKOUT_DIR/$r"
  DEST="$BASELINE_DIR/$r"
  [ -d "$CHECKOUT" ] || { echo "missing checkout: $CHECKOUT" >&2; exit 1; }

  echo "=== $r ==="
  rm -rf "$CHECKOUT/graphify-out"
  START=$(date +%s)
  # --no-cluster is the zero-LLM path: raw extraction only, no community labelling.
  "$GRAPHIFY" update "$CHECKOUT" --no-cluster >/dev/null 2>&1 || {
    echo "  graphify update failed for $r" >&2; exit 1; }
  ELAPSED=$(( $(date +%s) - START ))

  [ -f "$CHECKOUT/graphify-out/graph.json" ] || { echo "  no graph.json produced" >&2; exit 1; }

  mkdir -p "$DEST"
  rm -rf "$DEST"/*
  # Move dotfiles too (.graphify_root), then the rest.
  mv "$CHECKOUT/graphify-out"/.[!.]* "$DEST"/ 2>/dev/null || true
  mv "$CHECKOUT/graphify-out"/* "$DEST"/ 2>/dev/null || true
  rmdir "$CHECKOUT/graphify-out"

  # Refuse to report success while anything remains inside the parsed tree.
  if find "$CHECKOUT" -name graphify-out | grep -q .; then
    echo "  FAILED to clear graphify-out from $CHECKOUT" >&2; exit 1
  fi

  NODES=$(python3 -c "import json;d=json.load(open('$DEST/graph.json'));print(len(d['nodes']))")
  LINKS=$(python3 -c "import json;d=json.load(open('$DEST/graph.json'));print(len(d.get('links',[])))")
  echo "  ${ELAPSED}s  nodes=$NODES  links=$LINKS  -> $DEST/graph.json"
done
