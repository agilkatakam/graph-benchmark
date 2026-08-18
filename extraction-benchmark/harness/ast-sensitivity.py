#!/usr/bin/env python
"""Every scoring decision in this benchmark, re-scored with that decision turned OFF.

Each knob below is a choice the comparison makes that a reviewer could reasonably have made
differently. For each, both systems are re-scored with the choice removed and the delta reported
for BOTH sides, so the size and direction of every decision is visible.

  multiset      (file, name) multiset -> set. An overload one side collapses stops being a miss.
  bodyscope     TS/JS: truth restricted to declarations not inside another declaration's body.
  macro         C/C++: stop excluding declarations whose name collides with a macro name.
  normcpp       C/C++: stop stripping template argument lists and `Class::` qualification.
  pyi           Python: stop excluding `.pyi` stubs, which Graphify's file walk never opens.
  gitignored    Stop reading files that are committed to git but matched by .gitignore, which
                Graphify skips and both the referee and the extractor read.

  python3 harness/ast-sensitivity.py --knob macro
  python3 harness/ast-sensitivity.py --all
"""
import argparse
import collections
import importlib.util
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("alc", os.path.join(HERE, "ast-layer-compare.py"))
alc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alc)

_tspec = importlib.util.spec_from_file_location("apt", os.path.join(HERE, "ast-paper-tables.py"))
apt = importlib.util.module_from_spec(_tspec)
_tspec.loader.exec_module(apt)

TMP = os.environ.get("TMPDIR", "/tmp")

# Set by --corpus. A knob's size is corpus-dependent — the gitignore knob, for instance, is near
# zero on some corpora and material on others — so a knob measured on one corpus does not
# describe the others.
CORPORA = {
    "corpus-a": (os.path.join(TMP, "ast-bench"), ".corpus-a-checkouts", ".corpus-a-baselines"),
    "corpus-b": (os.path.join(TMP, "ast-bench-corpus-b"), ".corpus-b-checkouts",
                ".corpus-b-baselines"),
    "polyglot": (os.path.join(TMP, "ast-bench-poly"), ".polyglot-checkouts",
                 ".polyglot-baselines"),
}
WORK, CHECKOUTS, BASELINES = None, None, None
REPOS_FOR = None


def use_corpus(name):
    global WORK, CHECKOUTS, BASELINES, REPOS_FOR
    work, co, bl = CORPORA[name]
    WORK = work
    CHECKOUTS = os.path.join(ROOT, co)
    BASELINES = os.path.join(ROOT, bl)
    REPOS_FOR = {"corpus-a": apt.LANG_REPOS, "corpus-b": apt.CORPUS_B_REPOS,
                 "polyglot": {l: ["thrift", "protobuf"] for l in apt.LANGS}}[name]


def pct(a, b):
    return None if not b else round(100.0 * a / b, 2)


def load(lang, repo):
    """Truth, Koragraph nodes and decoded Graphify nodes for one language/repo, with the four
    planes already normalised the way ast-layer-compare.py normalises them.

    Returns None when the pair was never measured — the polyglot corpus does not carry every
    language in every repository, and a zero row for an unmeasured pair would read as a score."""
    tp = os.path.join(WORK, f"truth_{lang}__{repo}.json")
    if not os.path.exists(tp):
        return None
    truth = json.load(open(tp))
    kora = json.load(open(os.path.join(WORK, f"kora_{lang}__{repo}.json")))
    gf = alc.load_graphify(os.path.join(BASELINES, repo, "graph.json"), repo, lang,
                           os.path.join(CHECKOUTS, repo))
    return truth, kora, gf


def planes(truth, kora, gf, lang, *, apply_norm=True, drop_pyi=True, drop_disabled=True,
           drop_files=()):
    """Rebuild the scored planes. Mirrors ast-layer-compare.py's main() exactly except for the
    knobs — kept in one place so a knob cannot silently diverge from the real scorer."""
    t = {p: [dict(x) for x in truth.get(p, [])]
         for p in ("methods", "types", "fields", "constants")}
    k_nodes = list(kora["nodes"])
    g = {"methods": [dict(x) for x in gf["methods"]], "types": [dict(x) for x in gf["types"]],
         "fields": [dict(x) for x in gf.get("fields", [])]}

    if lang == "python" and drop_pyi:
        for p in t.values():
            p[:] = [x for x in p if not str(x["file"]).endswith(".pyi")]
        k_nodes = [n for n in k_nodes if not str(n["file"]).endswith(".pyi")]

    disabled = (truth.get("disabled_ranges") or {}) if drop_disabled else {}
    if disabled:
        def bad(n):
            ln = n.get("line")
            return ln is not None and any(lo <= ln <= hi for lo, hi in disabled.get(n["file"], ()))
        k_nodes = [n for n in k_nodes if not bad(n)]
        for p in list(t.values()) + list(g.values()):
            p[:] = [n for n in p if not bad(n)]

    if drop_files:
        drop = set(drop_files)
        k_nodes = [n for n in k_nodes if n["file"] not in drop]
        for p in list(t.values()) + list(g.values()):
            p[:] = [n for n in p if n["file"] not in drop]

    SQL_TYPES = ("DB_TABLE", "DB_VIEW", "DB_TYPE", "DB_TRIGGER")
    k = {
        "methods": [n for n in k_nodes if n["node_type"] == "METHOD"],
        "types": [n for n in k_nodes
                  if n["node_type"] in (SQL_TYPES if lang == "sql"
                                        else ("CLASS", "ENTITY", "INTERFACE"))
                  and n["kind"] not in ("anonymous_class", "enum_constant")],
        "fields": [n for n in k_nodes if n["node_type"] == "FIELD"],
        "constants": [n for n in k_nodes if n["node_type"] == "CONSTANT"],
    }
    k = {p: [{"file": n["file"], "name": n["name"], "line": n["line"]} for n in v]
         for p, v in k.items()}

    if apply_norm:
        fn = {"cpp": alc.norm_cpp, "kotlin": alc.norm_kotlin, "sql": alc.norm_sql}.get(lang)
        if fn:
            for group in list(t.values()) + list(k.values()) + list(g.values()):
                for item in group:
                    item["name"] = fn(item["name"])
            if lang == "sql":
                for group in list(t.values()) + list(k.values()) + list(g.values()):
                    group[:] = [x for x in group if x["name"]]
    return t, k, g


def score_plane(t_items, got_items, as_set=False):
    tm = alc.multiset([(x["file"], x["name"]) for x in t_items])
    gm = alc.multiset([(x["file"], x["name"]) for x in got_items])
    if as_set:
        tm = collections.Counter(set(tm)); gm = collections.Counter(set(gm))
    matched = sum((tm & gm).values())
    return matched, sum(tm.values()), sum(gm.values())


def head_to_head(t, k, g, lang, as_set=False):
    """Pooled methods + types+constants — the head-to-head denominator."""
    pool = lang in ("typescript", "javascript")
    tn = t["types"] + (t["constants"] if pool else [])
    kn = k["types"] + (k["constants"] if pool else [])
    acc = collections.Counter()
    for tp, kp, gp in ((t["methods"], k["methods"], g["methods"]), (tn, kn, g["types"])):
        km, tt, kf = score_plane(tp, kp, as_set)
        gm, _, gff = score_plane(tp, gp, as_set)
        acc["t"] += tt; acc["km"] += km; acc["kf"] += kf; acc["gm"] += gm; acc["gf"] += gff
    return acc


def method_only(t, k, g, as_set=False):
    km, tt, kf = score_plane(t["methods"], k["methods"], as_set)
    gm, _, gf = score_plane(t["methods"], g["methods"], as_set)
    return collections.Counter({"t": tt, "km": km, "kf": kf, "gm": gm, "gf": gf})


def report(title, note, rows):
    print(f"\n### {title}\n")
    print(note + "\n")
    print("| language | measure | with the decision | without it | Koragraph delta | Graphify delta |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")


def _fmt(v):
    return "—" if v is None else f"{v:.2f}%"


def _pair(on, off, measure):
    """One row: the measure with and without the decision, for each side."""
    if measure == "recall":
        k_on, k_off = pct(on["km"], on["t"]), pct(off["km"], off["t"])
        g_on, g_off = pct(on["gm"], on["t"]), pct(off["gm"], off["t"])
    else:
        k_on, k_off = pct(on["km"], on["kf"]), pct(off["km"], off["kf"])
        g_on, g_off = pct(on["gm"], on["gf"]), pct(off["gm"], off["gf"])
    def d(a, b):
        return "—" if a is None or b is None else f"{a - b:+.2f}"
    return (f"K {_fmt(k_on)} / G {_fmt(g_on)}", f"K {_fmt(k_off)} / G {_fmt(g_off)}",
            d(k_on, k_off), d(g_on, g_off))


def knob_multiset(langs):
    rows = []
    for lang in langs:
        on = collections.Counter(); off = collections.Counter()
        for repo in REPOS_FOR[lang]:
            args = load(lang, repo)
            if args is None:
                continue
            t, k, g = planes(*args, lang)
            on += method_only(t, k, g, as_set=False)
            off += method_only(t, k, g, as_set=True)
        a, b, kd, gd = _pair(on, off, "recall")
        rows.append([apt.PRETTY[lang], "method recall", a, b, kd, gd])
    report("Multiset vs set matching",
           "Multiset is the primary: an overload IS a distinct declaration. Graphify's node id is\n"
           "(scope, name) with no parameter list, so same-named declarations in a scope collapse by\n"
           "design. `without it` is set semantics — the modelling difference removed.", rows)


def knob_bodyscope(langs):
    rows = []
    for lang in langs:
        on = collections.Counter(); off = collections.Counter()
        for repo in REPOS_FOR[lang]:
            args = load(lang, repo)
            if args is None:
                continue
            t, k, g = planes(*args, lang)
            on += method_only(t, k, g)
            # `in_function_body` is stamped by the TypeScript-compiler referee off the parse
            # tree. It cannot be recovered from declaration spans: lodash wraps its whole source
            # in an anonymous IIFE, a function body holding most of the file's declarations and
            # not itself a declaration truth records.
            t2 = dict(t)
            t2["methods"] = [m for m in t["methods"] if not m.get("in_function_body")]
            off += method_only(t2, k, g)
        a, b, kd, gd = _pair(on, off, "recall")
        rows.append([apt.PRETTY[lang], "method recall", a, b, kd, gd])
    report("Declarations inside a function body",
           "Graphify's walk does not enter a `statement_block` to emit nodes; its own source cites\n"
           "its issue #1077. The taxonomy has no depth filter — a nested function is a real\n"
           "declaration and a reader needs it — so it is charged for a scope it is documented not\n"
           "to enter. `without it` restricts truth to declarations that are not inside any\n"
           "function body. A class body is not a function body: a method of a top-level class\n"
           "counts as top-scope here, and Graphify does emit those.", rows)


def knob_composition(langs):
    """How much of each repository sits inside a function body — the scope the `bodyscope` knob
    is about — so the size of that knob can be read against the corpus it was measured on."""
    print("\n### Corpus composition — truth methods inside a function body\n")
    print("| language | repo | truth methods | inside a function body |")
    print("|---|---|---|---|")
    worst = []
    for lang in langs:
        for repo in REPOS_FOR[lang]:
            t = json.load(open(os.path.join(WORK, f"truth_{lang}__{repo}.json")))["methods"]
            n = sum(1 for m in t if m.get("in_function_body"))
            if not t:
                continue
            worst.append((pct(n, len(t)), repo))
            print(f"| {apt.PRETTY[lang]} | {repo} | {len(t):,} | {n:,} ({pct(n, len(t)):.1f}%) |")
    worst.sort()
    print(f"\nRange across these repositories: {worst[0][1]} at {worst[0][0]:.1f}% "
          f"to {worst[-1][1]} at {worst[-1][0]:.1f}%.")


def knob_macro(langs):
    rows = []
    for lang in langs:
        on = collections.Counter(); off = collections.Counter()
        for repo in REPOS_FOR[lang]:
            args = load(lang, repo)
            if args is None:
                continue
            on += method_only(*planes(*args, lang), )
            off += method_only(*planes(*args, lang, drop_disabled=False))
        for measure in ("recall", "precision"):
            a, b, kd, gd = _pair(on, off, measure)
            rows.append([apt.PRETTY[lang], f"method {measure}", a, b, kd, gd])
    report("The C/C++ macro-line exclusion",
           "A declaration whose name collides with a macro name anywhere in the repository is\n"
           "excluded from both systems: a declaration written *through* a macro cannot be\n"
           "adjudicated by anything that has not expanded it.", rows)


def knob_normcpp(langs):
    rows = []
    for lang in langs:
        on = collections.Counter(); off = collections.Counter()
        for repo in REPOS_FOR[lang]:
            args = load(lang, repo)
            if args is None:
                continue
            on += method_only(*planes(*args, lang))
            off += method_only(*planes(*args, lang, apply_norm=False))
        for measure in ("recall", "precision"):
            a, b, kd, gd = _pair(on, off, measure)
            rows.append([apt.PRETTY[lang], f"method {measure}", a, b, kd, gd])
    report("Name normalisation (`norm_cpp`)",
           "Strips template argument lists and `Class::` qualification, on every side. Without it,\n"
           "one declaration spelled two ways scores as a miss AND a fabrication.", rows)


def knob_pyi(langs):
    rows = []
    for lang in langs:
        on = collections.Counter(); off = collections.Counter()
        for repo in REPOS_FOR[lang]:
            args = load(lang, repo)
            if args is None:
                continue
            on += head_to_head(*planes(*args, lang), lang)
            off += head_to_head(*planes(*args, lang, drop_pyi=False), lang)
        a, b, kd, gd = _pair(on, off, "recall")
        rows.append([apt.PRETTY[lang], "head-to-head recall", a, b, kd, gd])
    report("The `.pyi` stub exclusion",
           "`.pyi` is not in Graphify's CODE_EXTENSIONS (graphify/detect.py:31), so it never opens a\n"
           "stub file. Charging it recall for declarations it had no chance at is the skip-set\n"
           "asymmetry in the other direction.", rows)


def _gitignored_tracked(repo):
    """Files committed to git that .gitignore nonetheless matches. Graphify skips them; the
    referee and the extractor both walk the filesystem and read them."""
    d = os.path.join(CHECKOUTS, repo)
    out = subprocess.run(["git", "-C", d, "ls-files", "-c", "--ignored", "--exclude-standard"],
                         capture_output=True, text=True).stdout.split()
    return out


def knob_gitignored(langs):
    """Reported across ALL THREE corpora, not just the one `--corpus` selects: the exposure is
    concentrated in a few repositories, so a single corpus does not bound the effect.
    """
    saved = (WORK, CHECKOUTS, BASELINES, REPOS_FOR)
    rows = []
    try:
        for corpus in ("corpus-a", "corpus-b", "polyglot"):
            use_corpus(corpus)
            for lang in langs:
                on = collections.Counter(); off = collections.Counter()
                dropped = 0
                for repo in REPOS_FOR[lang]:
                    args = load(lang, repo)
                    if args is None:
                        continue
                    files = _gitignored_tracked(repo)
                    base = head_to_head(*planes(*args, lang), lang)
                    alt = head_to_head(*planes(*args, lang, drop_files=files), lang)
                    dropped += base["t"] - alt["t"]
                    on += base; off += alt
                # A language with nothing hidden this way is not a finding; printing ten
                # +0.00 rows per corpus buries the three that are not zero.
                if not dropped:
                    continue
                a, b, kd, gd = _pair(on, off, "recall")
                rows.append([corpus, f"{apt.PRETTY[lang]} ({dropped} declarations)", a, b, kd, gd])
    finally:
        globals().update(zip(("WORK", "CHECKOUTS", "BASELINES", "REPOS_FOR"), saved))
    print("\n### Files that are committed to git but matched by .gitignore\n")
    print("Every language-and-corpus cell where the effect is not exactly zero. All others are "
          "zero.\n")
    print("| corpus | language | with the decision | without it | Koragraph delta | Graphify delta |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")
    if not rows:
        print("| — | no cell is affected | — | — | — | — |")


def _referee_knob(lang, env, title, note, build_truth):
    """Two of the taxonomy decisions live inside a REFEREE, not inside the scorer, so measuring
    them needs the referee re-run with the rule off. Each referee carries an environment switch
    for exactly this; nothing else reads it."""
    rows = []
    alt_dir = os.path.join(TMP, f"ast-sens-{env.lower()}")
    os.makedirs(alt_dir, exist_ok=True)
    on = collections.Counter(); off = collections.Counter()
    for repo in REPOS_FOR[lang]:
        args = load(lang, repo)
        if args is None:
            continue
        alt_truth = os.path.join(alt_dir, f"truth_{lang}__{repo}.json")
        if not os.path.exists(alt_truth):
            build_truth(repo, alt_truth)
        truth, kora, gf = args
        on += head_to_head(*planes(truth, kora, gf, lang), lang)
        off += head_to_head(*planes(json.load(open(alt_truth)), kora, gf, lang), lang)
    for measure in ("recall", "precision"):
        a, b, kd, gd = _pair(on, off, measure)
        rows.append([apt.PRETTY[lang], f"head-to-head {measure}", a, b, kd, gd])
    report(title, note, rows)


def knob_sqldedupe(langs):
    def build(repo, out):
        env = dict(os.environ, AST_SQL_NO_DEDUPE="1")
        subprocess.run(["python3",
                        os.path.join(HERE, "ast-referee-sqlref.py"),
                        os.path.join(CHECKOUTS, repo), "--out", out],
                       env=env, check=True, capture_output=True)
    _referee_knob("sql", "SQLDEDUPE", "The SQL re-declaration dedupe",
                  "T-SQL's universal idiom creates an empty stub inside `EXEC('CREATE PROCEDURE\n"
                  "...')` and then ALTERs it with the real body. Both statements declare the same\n"
                  "routine, so truth records one node per (file, name) — which is also how\n"
                  "Graphify's own node id works. `without it` counts each statement separately.",
                  build)


def knob_bodyprototypes(langs):
    def build(repo, out):
        env = dict(os.environ, AST_KEEP_BODY_PROTOTYPES="1")
        subprocess.run(["python3",
                        os.path.join(HERE, "ast-referee-ctags.py"),
                        os.path.join(CHECKOUTS, repo), "--lang", "cpp", "--out", out],
                       env=env, check=True, capture_output=True)
    _referee_knob("cpp", "BODYPROTO", "C/C++ macro invocations inside a function body",
                  "ctags files a macro INVOCATION inside a function body as a prototype:\n"
                  "`CHECK_THROWS_WITH_AS(...)` in a test body looks exactly like `int foo(int);`\n"
                  "to a parser that has not expanded it. Truth drops a prototype tag at\n"
                  "function-body brace depth > 0. `without it` keeps them all.",
                  build)


KNOBS = {
    "multiset": (knob_multiset, ["java", "typescript", "javascript", "kotlin", "cpp"]),
    "sqldedupe": (knob_sqldedupe, ["sql"]),
    "bodyprototypes": (knob_bodyprototypes, ["cpp"]),
    "bodyscope": (knob_bodyscope, ["typescript", "javascript"]),
    "composition": (knob_composition, ["typescript", "javascript"]),
    "macro": (knob_macro, ["cpp"]),
    "normcpp": (knob_normcpp, ["cpp"]),
    "pyi": (knob_pyi, ["python"]),
    "gitignored": (knob_gitignored, apt.LANGS),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--knob", choices=sorted(KNOBS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--corpus", default="corpus-a", choices=sorted(CORPORA))
    a = ap.parse_args()
    if not (a.knob or a.all):
        ap.error("pass --knob NAME or --all")
    use_corpus(a.corpus)
    for name in (sorted(KNOBS) if a.all else [a.knob]):
        fn, langs = KNOBS[name]
        fn(langs)


if __name__ == "__main__":
    main()
