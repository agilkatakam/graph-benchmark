#!/usr/bin/env python3
"""Corpus-level aggregate for one language, from the per-repo comparison files ast-bench.sh
leaves in its work directory.

Usage:
  python3 harness/ast-aggregate.py --lang python [--work $TMPDIR/ast-bench]
"""
import argparse
import collections
import glob
import json
import os

# The declaration planes. RATIONALE and DOC_REF are comment-derived and are counted, not
# scored: no referee models them, so including them in a precision figure would score them
# against a truth set that cannot contain them.
DECL_NODE_TYPES = ("CLASS", "METHOD", "FIELD", "CONSTANT")

# SQL's type plane is the named schema objects, which carry the product node types rather than
# CLASS. Pooling SQL against the list above scored every table, view and column as a miss.
SQL_DECL_NODE_TYPES = DECL_NODE_TYPES + ("DB_TABLE", "DB_VIEW", "DB_TYPE", "DB_TRIGGER")

LANG_REPOS = {
    "java": ["spring-petclinic-rest", "gson", "spring-petclinic", "commons-lang", "netty", "mall", "dubbo"],
    "typescript": ["zod", "nest", "angular", "excalidraw", "vue-core"],
    "javascript": ["express", "axios", "lodash", "eslint", "webpack", "gatsby"],
    "python": ["django-machina", "fastapi", "flask", "poetry", "django", "pandas"],
    "csharp": ["sharex", "newtonsoft-json", "jellyfin", "efcore", "avalonia", "abp"],
    "sql": ["chinook-database", "frk", "test-db", "mssql-maintenance", "timescaledb", "citus"],
    "cpp": ["redis", "leveldb", "libuv", "googletest", "nlohmann-json", "curl"],
    "go": ["gin", "cobra", "grpc-go", "etcd", "hugo", "prometheus"],
    "php": ["monolog", "slim", "guzzle", "composer", "framework", "phpunit"],
    "kotlin": ["mockk", "arrow", "coroutines", "detekt", "exposed", "ktor"],
}


def _sql_norm(name):
    import importlib.util
    global _NORM
    try:
        return _NORM(name)
    except NameError:
        spec = importlib.util.spec_from_file_location(
            "cmp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ast-layer-compare.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _NORM = mod.norm_sql
        return _NORM(name)


def _pre_off(truth, item):
    for lo, hi in (truth.get("disabled_ranges") or {}).get(item["file"], ()):
        if item.get("line") is not None and lo <= item["line"] <= hi:
            return True
    return False


def pct(a, b):
    return 0.0 if not b else round(100.0 * a / b, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", required=True)
    ap.add_argument("--work", default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "ast-bench"))
    ap.add_argument("--repos", nargs="*")
    a = ap.parse_args()
    repos = a.repos or LANG_REPOS[a.lang]
    decl_types = SQL_DECL_NODE_TYPES if a.lang == "sql" else DECL_NODE_TYPES

    planes = {}
    files = ms = 0
    comments = collections.Counter()
    per_repo = []

    for r in repos:
        cmp_path = os.path.join(a.work, f"cmp_{a.lang}__{r}.json")
        if not os.path.exists(cmp_path):
            print(f"  (missing {cmp_path} — run ast-bench.sh --lang {a.lang} first)")
            continue
        d = json.load(open(cmp_path))
        files += d["truth_counts"]["files"]
        ms += d["extract_ms"]["kora"]
        for k, v in (d.get("comment_planes") or {}).items():
            comments[k] += v
        for e in d["entities"]:
            p = planes.setdefault(e["entity"], collections.Counter())
            p["truth"] += e["truth"]
            p["k_match"] += e["kora"]["matched"]
            p["k_found"] += e["kora"]["found"]
            p["k_miss"] += e["kora"]["missed"]
            p["k_extra"] += e["kora"]["extra"]
            p["g_match"] += e["graphify"]["matched"]
            p["g_found"] += e["graphify"]["found"]

        # Pooled symbol view: every declaration the referee found against every declaration
        # node each side emitted, with no plane classification involved.
        truth = json.load(open(os.path.join(a.work, f"truth_{r}.json")))
        kora = json.load(open(os.path.join(a.work, f"kora_{r}.json")))
        norm = (lambda v: v) if a.lang != "sql" else _sql_norm
        tm = collections.Counter((x["file"], norm(x["name"]))
                                 for pl in ("types", "methods", "fields", "constants")
                                 for x in truth.get(pl, []) if not _pre_off(truth, x))
        # Same exclusion the per-plane scoring applies: a declaration inside a
        # preprocessor-disabled region is real but no referee here can adjudicate it.
        disabled = truth.get("disabled_ranges") or {}

        def _off_limits(n):
            for lo, hi in disabled.get(n["file"], ()):
                if n.get("line") is not None and lo <= n["line"] <= hi:
                    return True
            return False

        km = collections.Counter((n["file"], norm(n["name"])) for n in kora["nodes"]
                                 if n["node_type"] in decl_types and not _off_limits(n))
        matched = sum((tm & km).values())
        per_repo.append((r, sum(tm.values()), pct(matched, sum(tm.values())),
                         pct(matched, sum(km.values())), matched, sum(km.values())))

    print(f"=== {a.lang}: {len(per_repo)} repos, {files} files, {ms} ms extraction ===")
    order = ["methods", "types", "constants", "types+constants", "fields"]
    for name in order:
        p = planes.get(name)
        if not p or not p["truth"]:
            continue
        # Graphify is scored on `methods` and `types+constants` only — its label convention
        # yields nothing finer. The other rows have no Graphify column at all; printing 0.0%
        # there would understate it rather than report a miss.
        g = (f"{pct(p['g_match'], p['truth']):>6}% / {pct(p['g_match'], p['g_found']):>6}%"
             if p["g_found"] else "   not scored on this row")
        print(f"  {name:<16} truth={p['truth']:<7} KORA {pct(p['k_match'], p['truth']):>6}% /"
              f" {pct(p['k_match'], p['k_found']):>6}%   miss={p['k_miss']:<5} extra={p['k_extra']:<5} | GFY {g}")

    print("  pooled symbols (no plane classification):")
    for r, t, rec, prec, _m, _f in per_repo:
        print(f"    {r:<18} truth={t:<7} KORA {rec}% / {prec}%")
    tt = sum(x[1] for x in per_repo)
    tm_ = sum(x[4] for x in per_repo)
    tf_ = sum(x[5] for x in per_repo)
    print(f"    {'ALL':<18} truth={tt:<7} KORA {pct(tm_, tt)}% / {pct(tm_, tf_)}%")
    # Graphify pooled: its two scored buckets (callables, and the non-callable bucket that
    # holds types and constants together) against the same pooled truth.
    g_match = sum(planes[n]["g_match"] for n in ("methods", "types+constants") if n in planes)
    g_found = sum(planes[n]["g_found"] for n in ("methods", "types+constants") if n in planes)
    print(f"    {'ALL':<18} truth={tt:<7} GFY  {pct(g_match, tt)}% / {pct(g_match, g_found)}%")
    print(f"  comment planes: {dict(comments)}")


if __name__ == "__main__":
    main()
