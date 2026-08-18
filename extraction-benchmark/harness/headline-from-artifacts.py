#!/usr/bin/env python3
"""Recompute every published extraction figure from the committed artifacts.

Reads results/artifacts/ only — no checkouts, no referees, no baselines, no Koragraph. Anything
printed here is arithmetic over JSON that ships in this repository, so a reader can check the
headline without running either system.

  python3 harness/headline-from-artifacts.py [--artifacts results/artifacts]

HEAD-TO-HEAD PLANES
-------------------
The headline pools the `methods` and `types+constants` planes: the planes both systems model in
every language. The field plane is reported separately because Graphify models no field plane in
7 of the 10 languages, so pooling it would score an absent feature as a recall miss.

Rows are counted only where `graphify_scored` is true. `types` and `constants` appear as separate
diagnostic rows that re-partition the same declarations as `types+constants`; counting them too
would double-count.
"""
import argparse
import collections
import glob
import json
import os

HEAD_TO_HEAD = ("methods", "types+constants")
CORPUS_DIRS = {"corpus A": "ast-bench", "corpus B": "ast-bench-corpus-b",
               "polyglot": "ast-bench-poly"}


def rows(artifacts, dirs, planes, scored_only=True):
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(artifacts, d, "cmp_*.json"))):
            lang = os.path.basename(path)[len("cmp_"):].split("__")[0]
            doc = json.load(open(path))
            for e in doc["entities"]:
                if e["entity"] not in planes:
                    continue
                if scored_only and not e["graphify_scored"]:
                    continue
                yield lang, doc["repo"], e


def pct(num, den):
    return 100.0 * num / den if den else 0.0


def pool(records):
    t = collections.Counter()
    for _, _, e in records:
        t["truth"] += e["truth"]
        t["k_matched"] += e["kora"]["matched"]
        t["k_found"] += e["kora"]["found"]
        t["g_matched"] += e["graphify"]["matched"]
        t["g_found"] += e["graphify"]["found"]
    return t


def line(label, t):
    print(f"{label:22s} truth {t['truth']:>9,}   "
          f"Koragraph {pct(t['k_matched'], t['truth']):6.2f}% / {pct(t['k_matched'], t['k_found']):6.2f}%   "
          f"Graphify {pct(t['g_matched'], t['truth']):6.2f}% / {pct(t['g_matched'], t['g_found']):6.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "artifacts"))
    a = ap.parse_args()

    headline_dirs = [CORPUS_DIRS["corpus A"], CORPUS_DIRS["corpus B"]]

    print("HEADLINE — head-to-head planes, corpus A + corpus B")
    print("                       (recall / precision)")
    t = pool(rows(a.artifacts, headline_dirs, HEAD_TO_HEAD))
    line("pooled", t)
    print(f"{'':22s} declarations Graphify misses that Koragraph finds: "
          f"{t['k_matched'] - t['g_matched']:,}")

    print("\nBy language")
    by_lang = collections.defaultdict(list)
    for lang, repo, e in rows(a.artifacts, headline_dirs, HEAD_TO_HEAD):
        by_lang[lang].append((lang, repo, e))
    for lang in sorted(by_lang):
        line(lang, pool(by_lang[lang]))

    print("\nField plane — Koragraph only where Graphify models no plane")
    f = pool(rows(a.artifacts, headline_dirs, ("fields",), scored_only=False))
    print(f"{'fields':22s} truth {f['truth']:>9,}   "
          f"Koragraph {pct(f['k_matched'], f['truth']):6.2f}% / "
          f"{pct(f['k_matched'], f['k_found']):6.2f}%")

    print("\nPolyglot corpus")
    line("polyglot", pool(rows(a.artifacts, [CORPUS_DIRS["polyglot"]], HEAD_TO_HEAD)))

    print("\nPer-repository cells — head-to-head recall")
    cells = collections.defaultdict(lambda: collections.Counter())
    for lang, repo, e in rows(a.artifacts, headline_dirs + [CORPUS_DIRS["polyglot"]],
                              HEAD_TO_HEAD):
        c = cells[(lang, repo)]
        c["truth"] += e["truth"]
        c["k"] += e["kora"]["matched"]
        c["g"] += e["graphify"]["matched"]
    losses = [k for k, c in cells.items() if c["g"] >= c["k"]]
    print(f"cells: {len(cells)}   cells where Graphify's recall >= Koragraph's: {len(losses)}")
    for k in sorted(losses):
        c = cells[k]
        print(f"  {k[0]}/{k[1]}: Koragraph {pct(c['k'], c['truth']):.2f}% "
              f"Graphify {pct(c['g'], c['truth']):.2f}%")


if __name__ == "__main__":
    main()
