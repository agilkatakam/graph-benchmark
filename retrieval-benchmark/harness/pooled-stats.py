#!/usr/bin/env python3
"""Per-cell table and pooled paired statistics, recomputed from results/finalN.json.

Reads the scored results only, so every published retrieval figure can be checked without
re-running either system.

  python3 harness/pooled-stats.py [--results results/finalN.json] [--budgets 1000,4000,8000]

The pooled figure is a paired comparison: both systems answer the same question at the same
budget, so the unit is the per-question difference, not the two means. The confidence interval is
a bootstrap over those differences under a fixed seed, and W/L/T counts how many questions each
system wins outright.
"""
import argparse
import collections
import json
import random

BOOTSTRAP_SEED = 20260817
BOOTSTRAP_N = 10000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results/finalN.json")
    ap.add_argument("--budgets", default="1000,4000,8000")
    ap.add_argument("--reference", default="graphify",
                    help="system the pooled difference is measured against")
    ap.add_argument("--system", default="koragraph")
    a = ap.parse_args()

    budgets = [int(b) for b in a.budgets.split(",")]
    rows = json.load(open(a.results))["rows"]
    paired = collections.defaultdict(dict)
    for r in rows:
        paired[(r["language"], r["repo"], r["qid"], r["budget"])][r["system"]] = r

    cells = collections.defaultdict(lambda: collections.defaultdict(list))
    for (lang, repo, _, budget), bysys in paired.items():
        for sysname, r in bysys.items():
            cells[(lang, repo, budget)][sysname].append(r)

    print(f"{'language':8s} {'repo':13s} {'n':>4s} {'budget':>7s}   "
          f"{'recall K':>8s} {'recall G':>8s}   {'attrib K':>8s} {'attrib G':>8s}")
    for (lang, repo, budget) in sorted(cells):
        if budget not in budgets:
            continue
        c = cells[(lang, repo, budget)]
        k, g = c[a.system], c[a.reference]
        print(f"{lang:8s} {repo:13s} {len(k):>4d} {budget:>7d}   "
              f"{sum(x['recall'] for x in k)/len(k):>8.3f} "
              f"{sum(x['recall'] for x in g)/len(g):>8.3f}   "
              f"{sum(x['recall_file'] for x in k)/len(k):>8.3f} "
              f"{sum(x['recall_file'] for x in g)/len(g):>8.3f}")

    print("\nPooled, paired by question")
    for budget in budgets:
        diffs = [v[a.system]["recall"] - v[a.reference]["recall"]
                 for key, v in paired.items()
                 if key[3] == budget and a.system in v and a.reference in v]
        if not diffs:
            continue
        n = len(diffs)
        mean = sum(diffs) / n
        wins = sum(1 for x in diffs if x > 0)
        losses = sum(1 for x in diffs if x < 0)
        rnd = random.Random(BOOTSTRAP_SEED)
        boots = sorted(sum(rnd.choice(diffs) for _ in range(n)) / n
                       for _ in range(BOOTSTRAP_N))
        lo = boots[int(0.025 * BOOTSTRAP_N)]
        hi = boots[int(0.975 * BOOTSTRAP_N)]
        print(f"  budget {budget:>5d}: n={n}  mean {mean:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  "
              f"{wins}W / {losses}L / {n - wins - losses}T")


if __name__ == "__main__":
    main()
