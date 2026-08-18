#!/usr/bin/env python3
"""Run every question against Graphify at every budget, and record the returned context.

FIDELITY
--------
This does not reimplement Graphify's retrieval. It loads graph.json exactly as
`graphify query` does (cli.py:852-945 — undirected graph, per-edge _src/_tgt preserved) and
calls Graphify's own `_query_graph_text(G, question, mode="bfs", depth=2, token_budget=B)`.
The only difference from shelling out is that the graph is parsed once per repository instead of
once per query, which is a speed optimisation in *their* favour and changes no output.

`--verify-cli` proves that: it re-runs a sample of queries through the real CLI binary and
requires byte-identical output. Phase 1's rule — read the competitor off its source, never infer
it from its score — applies to how we invoke it too.

GRAPHIFY'S QUERY OUTPUT IS NON-DETERMINISTIC — MEASURED 2026-08-17
------------------------------------------------------------------
Three identical `graphify query` invocations on the same graph returned 6525, 6619 and 6549
characters, with different edge sets. Cause, confirmed by pinning: `PYTHONHASHSEED`. `_bfs()`
returns a `set` of nodes and the edge list is built by iterating it, so Python's per-process
string-hash randomisation changes which edges are rendered — and therefore which survive the
character-budget cut. `PYTHONHASHSEED=0` makes it byte-identical across runs; seeds 1, 7 and 42
each give a different result.

This is handled, not hidden:

  * every run pins `PYTHONHASHSEED` and records it, so a third party reproduces our Graphify
    column exactly. The script REFUSES to run without it.
  * the seed sweep (several seeds over the same questions) measures Graphify's own run-to-run
    variance, and that band is published. A Koragraph lead smaller than Graphify's own seed
    noise is not a win, and will not be reported as one.

Usage:
  PYTHONHASHSEED=0 run-graphify.py --questions <q.json> --graph <graph.json> --out <ctx.json>
                                   [--budgets 1000,2000,4000,8000,16000,32000] [--verify-cli 5]
"""
import argparse
import json
import os
import subprocess
import sys
import time

# Run this with the venv built from requirements.txt: graphifyy is installed there, so the
# `graphify` binary sits beside the interpreter running this script. GRAPHIFY_VENV overrides it
# for a graphify installed somewhere else.
VENV = os.environ.get("GRAPHIFY_VENV", sys.prefix)


def load_graph(graph_path):
    """Byte-for-byte the loader in cli.py's `query` branch."""
    import networkx as nx  # noqa: F401
    from networkx.readwrite import json_graph

    with open(graph_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if "links" not in raw and "edges" in raw:
        raw = dict(raw, links=raw["edges"])
    raw = dict(raw, links=[{**l, "_src": l.get("source"), "_tgt": l.get("target")}
                           for l in raw.get("links", [])])
    try:
        return json_graph.node_link_graph(raw, edges="links")
    except TypeError:
        return json_graph.node_link_graph(raw)


def graphify_version():
    out = subprocess.run([os.path.join(VENV, "bin", "graphify"), "--version"],
                         capture_output=True, text=True)
    return (out.stdout or out.stderr).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--graph", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--budgets", default="1000,2000,4000,8000,16000,32000")
    ap.add_argument("--verify-cli", type=int, default=3,
                    help="re-run N queries through the real CLI and require identical output")
    # `graphify query` hardcodes depth=2 (cli.py:949) and exposes no flag, but
    # _query_graph_text defaults to 3 (serve.py:956) and the MCP tool schema defaults to 3 with a
    # documented max of 6 (serve.py:1204,1339). Benchmarking only the CLI's minimum understates
    # Graphify on the surface that actually competes here. Depth is swept, not assumed.
    ap.add_argument("--depth", type=int, default=2,
                    help="traversal depth (CLI hardcodes 2; function and MCP default to 3, max 6)")
    args = ap.parse_args()

    seed = os.environ.get("PYTHONHASHSEED")
    if seed is None or seed == "random":
        raise SystemExit(
            "FATAL: PYTHONHASHSEED must be pinned before the interpreter starts.\n"
            "Graphify's query output varies run-to-run with the hash seed (measured 2026-08-17: "
            "6525/6619/6549 chars for one identical query). An unpinned run is not reproducible "
            "and would silently give Graphify a different score each time.\n"
            "Re-run as:  PYTHONHASHSEED=0 .venv-graphify/bin/python3 " + " ".join(sys.argv))

    budgets = [int(b) for b in args.budgets.split(",")]
    with open(args.questions) as fh:
        qs = json.load(fh)

    from graphify.serve import _query_graph_text

    t_load = time.perf_counter()
    G = load_graph(args.graph)
    load_s = time.perf_counter() - t_load

    results = []
    for q in qs["questions"]:
        per_budget = {}
        for b in budgets:
            t0 = time.perf_counter()
            c0 = time.process_time()
            text = _query_graph_text(G, q["question"], mode="bfs", depth=args.depth,
                                     token_budget=b, context_filters=[])
            wall = time.perf_counter() - t0
            cpu = time.process_time() - c0
            if not text or not text.strip():
                # Fail loudly. Phase 1 lost an entire C/C++ result to a component that
                # emitted nothing and exited 0.
                raise SystemExit(
                    f"FATAL: Graphify returned empty context for {q['id']} at budget {b}. "
                    f"Refusing to record an empty result as a measurement.")
            per_budget[str(b)] = {"context": text, "chars": len(text),
                                  "wall_s": round(wall, 5), "cpu_s": round(cpu, 5)}
        results.append({"id": q["id"], "budgets": per_budget})

    # Verify the in-process path against the real CLI on a sample. The CLI cannot express a
    # depth other than 2, so at other depths this check is skipped rather than faked.
    verified = []
    verify_n = args.verify_cli if args.depth == 2 else 0
    if args.depth != 2 and args.verify_cli:
        sys.stderr.write(f"[graphify] depth={args.depth}: CLI verification skipped "
                         f"(`graphify query` hardcodes depth=2, cli.py:949)\n")
    for q in qs["questions"][:verify_n]:
        p = subprocess.run(
            [os.path.join(VENV, "bin", "graphify"), "query", q["question"],
             "--budget", "2000", "--graph", args.graph],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed})
        cli_out = p.stdout
        inproc = next(r for r in results if r["id"] == q["id"])["budgets"]["2000"]["context"]
        match = inproc.strip() in cli_out
        verified.append({"id": q["id"], "cli_matches_inprocess": match})
        if not match:
            raise SystemExit(
                f"FATAL: in-process Graphify output differs from the CLI for {q['id']}.\n"
                f"--- in-process ---\n{inproc[:600]}\n--- cli ---\n{cli_out[:600]}")

    out = {
        "system": "graphify",
        "system_version": graphify_version(),
        "graph": os.path.relpath(args.graph, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "language": qs["language"], "repo": qs["repo"], "pinned_sha": qs["pinned_sha"],
        "budgets": budgets,
        "python_hash_seed": seed,
        "depth": args.depth,
        "invocation": f"graphify.serve._query_graph_text(G, q, mode='bfs', depth={args.depth}, "
                      f"token_budget=B, context_filters=[])"
                      + (" — identical to `graphify query`" if args.depth == 2
                         else " — depth above the CLI's hardcoded 2; matches serve.py/MCP defaults"),
        "graph_load_s": round(load_s, 3),
        "cli_verification": verified,
        "results": results,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2)
    sys.stderr.write(f"[graphify:{qs['repo']}] {len(results)} questions x {len(budgets)} budgets, "
                     f"{len(verified)} CLI-verified -> {args.out}\n")


if __name__ == "__main__":
    main()
