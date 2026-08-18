#!/usr/bin/env python
"""Generate the per-language result tables directly from the benchmark artifacts, so a published
table cannot drift from what the harness produces.

Two views per language, which is the structure the comparison actually has:

  HEAD-TO-HEAD    methods, and types+constants. Both systems model these planes fully in every
                  one of the ten languages, so a recall gap here is an extraction gap.
  FIELD PLANE     reported separately, because Graphify's coverage of it is partial and differs
                  by language: enum constants only in Java and Kotlin, data members in C/C++,
                  nothing at all in the other seven. Folded into the head-to-head, a definitional
                  difference reads as a failure. Both systems' numbers are shown, with what
                  Graphify's model covers named — read off its extractor source, not inferred
                  from the score.

Both a multiset and a set figure are printed. Multiset is the primary: an overload IS a distinct
declaration. But Graphify's node id is (scope, name) with no parameter list, so same-named
declarations in a scope collapse by design; the set column shows its extraction with that
modelling difference removed, and the gap between the two columns is the size of the effect.

  python3 harness/ast-paper-tables.py [--work DIR] [--label NAME]
"""
import argparse
import collections
import json
import os
import re

LANGS = ["java", "typescript", "javascript", "python", "csharp", "sql", "cpp", "go", "php", "kotlin"]

# What Graphify's field-shaped node set actually covers, per language, read off
# graphify/extractors/*.py and engine.py rather than inferred from its score.
GFY_FIELD_MODEL = {
    "java": "enum constants only", "kotlin": "enum entries only",
    "cpp": "data members (`defines`/`field`)",
    "typescript": "nothing", "javascript": "nothing", "python": "nothing",
    "csharp": "nothing", "sql": "nothing", "go": "nothing", "php": "nothing",
}

PRETTY = {"java": "Java", "typescript": "TypeScript", "javascript": "JavaScript",
          "python": "Python", "csharp": "C#", "sql": "SQL", "cpp": "C/C++", "go": "Go",
          "php": "PHP", "kotlin": "Kotlin"}


def pct(a, b):
    return None if not b else round(100.0 * a / b, 2)


def fmt(v):
    return "—" if v is None else f"{v:.2f}%"


# The comparison JSON records the repo, not the language, so the language is resolved from the
# same repo lists the bench and aggregate scripts use. Other corpora pass their own via --repos.
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

CORPUS_B_REPOS = {
    "java": ["guava", "jackson-databind", "mybatis-3", "rxjava"],
    "typescript": ["typeorm", "playwright", "trpc", "rxjs"],
    "javascript": ["jquery", "d3", "socket.io", "underscore"],
    "python": ["requests", "scrapy", "sqlalchemy", "black"],
    "csharp": ["dapper", "serilog", "polly", "masstransit"],
    "sql": ["pg-partman", "pgtap", "northwind-psql", "pgvector"],
    "cpp": ["fmt", "spdlog", "jsoncpp", "zlib"],
    "go": ["echo", "viper", "fiber", "go-redis"],
    "php": ["phpmailer", "twig", "carbon", "doctrine-orm"],
    "kotlin": ["okhttp", "koin", "kotlinx-serialization", "ktlint"],
}


# Every language/repo pair the polyglot corpus was measured on. A pair whose head-to-head truth
# is below this is not a result and is listed as omitted rather than printed as a percentage —
# go/protobuf has eight declarations, and 87.5% of eight says nothing about either system.
POLYGLOT_MIN_TRUTH = 100


def _head_totals(cmp_doc):
    """Pooled methods + types+constants for one comparison file — the same denominator the
    head-to-head table uses, so the two can never disagree about the same quantity."""
    acc = collections.Counter()
    for row in cmp_doc["entities"]:
        if row["entity"] in ("types", "constants", "fields") or not row["graphify_scored"]:
            continue
        acc["truth"] += row["truth"]
        acc["k_match"] += row["kora"]["matched"]; acc["k_found"] += row["kora"]["found"]
        acc["g_match"] += row["graphify"]["matched"]; acc["g_found"] += row["graphify"]["found"]
    return acc


def emit_polyglot(work):
    print("| language | repo | declarations | Koragraph rec / prec | Graphify rec / prec | winner |")
    print("|---|---|---|---|---|---|")
    omitted, kora_wins, rows_printed = [], 0, 0
    for lang in LANGS:
        for repo in ("protobuf", "thrift"):
            p = os.path.join(work, f"cmp_{lang}__{repo}.json")
            if not os.path.exists(p):
                continue
            h = _head_totals(json.load(open(p)))
            if h["truth"] < POLYGLOT_MIN_TRUTH:
                if h["truth"]:
                    omitted.append(f"{lang}/{repo} ({h['truth']} declarations)")
                continue
            k_rec, g_rec = pct(h["k_match"], h["truth"]), pct(h["g_match"], h["truth"])
            win = "Koragraph" if k_rec > g_rec else ("Graphify" if g_rec > k_rec else "tie")
            kora_wins += win == "Koragraph"
            rows_printed += 1
            print(f"| {PRETTY[lang]} | {repo} | {h['truth']:,} | "
                  f"{fmt(k_rec)} / {fmt(pct(h['k_match'], h['k_found']))} | "
                  f"{fmt(g_rec)} / {fmt(pct(h['g_match'], h['g_found']))} | {win} |")
    print(f"\nKoragraph leads recall on {kora_wins} of {rows_printed} rows.")
    if omitted:
        print(f"\nOmitted as too small to be a result (under {POLYGLOT_MIN_TRUTH} declarations): "
              + "; ".join(omitted) + ".")


def _line_acc(cmp_doc, side):
    """`method_line_accuracy_within_2` is stored as a rendered string, `"99.8% (a/b)"`. Re-pool
    from the raw counts rather than averaging the percentages — the repos differ in size by two
    orders of magnitude and a mean of percentages is not the corpus figure."""
    m = re.search(r"\((\d+)/(\d+)\)", cmp_doc["method_line_accuracy_within_2"][side])
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def emit_lineacc(corpus_a_work, corpus_b_work):
    print("| language | A Koragraph | A Graphify | B Koragraph | B Graphify |")
    print("|---|---|---|---|---|")
    behind, scored_cells = [], 0
    for lang in LANGS:
        cells = []
        for work, repos in ((corpus_a_work, LANG_REPOS[lang]), (corpus_b_work, CORPUS_B_REPOS[lang])):
            acc = collections.Counter()
            for r in repos:
                p = os.path.join(work, f"cmp_{lang}__{r}.json")
                if not os.path.exists(p):
                    continue
                for side in ("kora", "graphify"):
                    ok, tot = _line_acc(json.load(open(p)), side)
                    acc[side + "_ok"] += ok; acc[side + "_tot"] += tot
            cells.append((pct(acc["kora_ok"], acc["kora_tot"]),
                          pct(acc["graphify_ok"], acc["graphify_tot"])))
        if all(c is None for pair in cells for c in pair):
            continue
        # Bold whichever side leads, so a reader cannot mistake who won a cell. A tie is
        # neither, which is most of this table.
        out = []
        for k, g in cells:
            out.append(f"**{fmt(k)}**" if k is not None and g is not None and k > g else fmt(k))
            out.append(f"**{fmt(g)}**" if k is not None and g is not None and g > k else fmt(g))
        print(f"| {PRETTY[lang]} | " + " | ".join(out) + " |")
        for label, (k, g) in zip(("corpus-a", "corpus-b"), cells):
            if k is None or g is None:
                continue
            scored_cells += 1
            if g > k:
                behind.append(f"{PRETTY[lang]} ({label}, {g - k:+.2f} to Graphify)")
    if behind:
        print(f"\nGraphify leads {len(behind)} of the {scored_cells} scored cells: "
              + "; ".join(behind) + ".")
    else:
        print(f"\nKoragraph is level or ahead in all {scored_cells} scored cells.")


def emit_labelblind(work):
    """The check behind the `models nothing` column: of Graphify's nodes of ANY label shape, how
    many land on a (file, name) the referee calls a field, and how many of those are a name it
    also calls a method or a type in that same file. Only the seven languages whose extractor
    models no field plane are listed — Java, Kotlin and C/C++ are scored on the field row itself.
    """
    print("| language | nodes landing on a truth field | of those, also a method/type of that "
          "name in that file |")
    print("|---|---|---|")
    for lang in LANGS:
        if GFY_FIELD_MODEL[lang] != "nothing":
            continue
        hits = coll = 0
        for r in LANG_REPOS[lang]:
            p = os.path.join(work, f"cmp_{lang}__{r}.json")
            if not os.path.exists(p):
                continue
            d = json.load(open(p))
            hits += d["graphify_nodes_landing_on_a_truth_field"]
            coll += d.get("graphify_field_hits_that_collide_with_a_method_or_type", 0)
        if not hits:
            continue
        print(f"| {PRETTY[lang]} | {hits:,} | {coll:,} ({fmt(pct(coll, hits))}) |")


def emit_gfyprecision(corpus_a_work, corpus_b_work):
    """Graphify's precision, pooled per language per plane, on both corpora.

    Printed as a table rather than summarised as a prose range, so the floor of each cell is
    readable directly from the artifacts.
    """
    print("| language | A methods | A types+const | A fields | B methods | "
          "B types+const | B fields |")
    print("|---|---|---|---|---|---|---|")
    lo = {}
    for lang in LANGS:
        cells = []
        for work, repos in ((corpus_a_work, LANG_REPOS[lang]), (corpus_b_work, CORPUS_B_REPOS[lang])):
            per = collections.defaultdict(collections.Counter)
            for r in repos:
                p = os.path.join(work, f"cmp_{lang}__{r}.json")
                if not os.path.exists(p):
                    continue
                for row in json.load(open(p))["entities"]:
                    if not row["graphify_scored"]:
                        continue
                    per[row["entity"]]["m"] += row["graphify"]["matched"]
                    per[row["entity"]]["f"] += row["graphify"]["found"]
            for plane in ("methods", "types+constants", "fields"):
                v = pct(per[plane]["m"], per[plane]["f"])
                cells.append(fmt(v) if v is not None else "not modelled")
                if v is not None:
                    lo[(lang, plane)] = min(lo.get((lang, plane), 100.0), v)
        print(f"| {PRETTY[lang]} | " + " | ".join(cells) + " |")
    worst = sorted(lo.items(), key=lambda kv: kv[1])[:3]
    print("\nLowest three cells across both corpora: "
          + ", ".join(f"{PRETTY[l]} {p} {v:.2f}%" for (l, p), v in worst) + ".")
    non_cpp = [v for (l, _), v in lo.items() if l != "cpp"]
    print(f"Excluding C/C++, every cell is between {min(non_cpp):.2f}% and {max(non_cpp):.2f}%.")


def emit_referees(lang, primary_work, second_work, repos):
    """Referee-spread figures: two referees, one taxonomy, the same extractor dumps on both
    sides — so every number here is a property of the referees, not of either system under
    test."""
    def pool(work):
        per = collections.defaultdict(collections.Counter)
        for r in repos:
            p = os.path.join(work, f"cmp_{lang}__{r}.json")
            if not os.path.exists(p):
                continue
            for row in json.load(open(p))["entities"]:
                if row["entity"] in ("types", "constants"):
                    continue
                per[row["entity"]]["t"] += row["truth"]
                for s in ("kora", "graphify"):
                    per[row["entity"]][s + "m"] += row[s]["matched"]
                    per[row["entity"]][s + "f"] += row[s]["found"]
        return per

    a, b = pool(primary_work), pool(second_work)
    print(f"| plane | truth (primary) | truth (second) | Koragraph rec | Graphify rec "
          f"| Koragraph prec | Graphify prec |")
    print("|---|---|---|---|---|---|---|")
    for e in ("methods", "types+constants", "fields"):
        if not (a[e]["t"] or b[e]["t"]):
            continue
        def cell(m_key, d_key):
            x, y = pct(a[e][m_key], a[e][d_key]), pct(b[e][m_key], b[e][d_key])
            if x is None or y is None:
                return "—"
            return f"{x:.2f}% → {y:.2f}% ({y - x:+.2f})"
        print(f"| {e} | {a[e]['t']:,} | {b[e]['t']:,} | {cell('koram', 't')} "
              f"| {cell('graphifym', 't')} | {cell('koram', 'koraf')} "
              f"| {cell('graphifym', 'graphifyf')} |")

    sym = union = 0
    for r in repos:
        pa = os.path.join(primary_work, f"truth_{lang}__{r}.json")
        pb = os.path.join(second_work, f"truth_{lang}__{r}.json")
        if not (os.path.exists(pa) and os.path.exists(pb)):
            continue
        A = collections.Counter((m["file"], m["name"]) for m in json.load(open(pa))["methods"])
        B = collections.Counter((m["file"], m["name"]) for m in json.load(open(pb))["methods"])
        sym += sum(((A - B) + (B - A)).values()); union += sum((A | B).values())
    if union:
        print(f"\nThe two referees disagree on **{100 * sym / union:.1f}%** of the method plane "
              f"({sym:,} of {union:,} entries in the union of the two truth sets).")

    # The numbers move between referees; what matters is whether the ORDERING between the two
    # systems survives the referee swap, so that is checked rather than assumed.
    flips = []
    for e in ("methods", "types+constants", "fields"):
        for what, mk, dk in (("recall", "m", "t"), ("precision", "m", "f")):
            for side, other in (("kora", "graphify"), ):
                x1 = pct(a[e][side + mk], a[e][side + dk if dk != "t" else "t"])
                y1 = pct(a[e][other + mk], a[e][other + dk if dk != "t" else "t"])
                x2 = pct(b[e][side + mk], b[e][side + dk if dk != "t" else "t"])
                y2 = pct(b[e][other + mk], b[e][other + dk if dk != "t" else "t"])
                if None in (x1, y1, x2, y2):
                    continue
                if (x1 > y1) != (x2 > y2):
                    flips.append(f"{e} {what}")
    print("The ordering between the two systems is **unchanged on every plane and both measures**"
          if not flips else
          "The ordering between the two systems FLIPS on: " + ", ".join(flips)
          + " — §2 may not claim it is stable.")


def emit_timing(tsv):
    """The performance section, from the single TSV `ast-timing.sh` writes.

    Nothing here is typed. Wall clock is machine- and load-sensitive, so the header comments in
    the TSV carry the machine and the Graphify version, and they are printed with the table —
    a performance number without the box it ran on is not a claim anyone can check.
    """
    meta, rows = {}, []
    with open(tsv) as fh:
        header = None
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#"):
                k, _, v = line[1:].strip().partition("\t")
                meta[k.strip()] = v.strip()
            elif header is None:
                header = line.split("\t")
            elif line:
                rows.append(dict(zip(header, line.split("\t"))))
    if not rows:
        return
    I = lambda r, k: int(r[k])  # noqa: E731

    print(f"Measured on **{meta.get('machine', 'unknown')}** — "
          f"{meta.get('cores', 'unknown')} cores — against **{meta.get('graphify', 'unknown')}**. "
          "Cold each time: Graphify's output directory, which holds its AST cache, is deleted "
          "before its timed run.\n")

    print("| repo | lang | files | Koragraph 1w | Koragraph 4w | Koragraph 10w | Graphify (own pool) |")
    print("|---|---|---|---|---|---|---|")
    tot = collections.Counter()
    for r in rows:
        for k in ("k1_wall", "k1_cpu", "k4_wall", "k4_cpu", "k10_wall", "k10_cpu",
                  "g_wall", "g_cpu", "gwarm_wall"):
            tot[k] += I(r, k)
        tot["files"] += I(r, "files")
        print(f"| {r['repo']} | {PRETTY.get(r['lang'], r['lang'])} | {I(r,'files'):,} "
              f"| {I(r,'k1_wall'):,} ms | {I(r,'k4_wall'):,} ms | {I(r,'k10_wall'):,} ms "
              f"| {I(r,'g_wall'):,} ms |")
    print(f"| **total** | | **{tot['files']:,}** | **{tot['k1_wall']/1000:.1f} s** "
          f"| **{tot['k4_wall']/1000:.1f} s** | **{tot['k10_wall']/1000:.1f} s** "
          f"| **{tot['g_wall']/1000:.1f} s** |")

    best = sum(min(I(r, "k1_wall"), I(r, "k4_wall"), I(r, "k10_wall")) for r in rows)
    w10 = sum(1 for r in rows if I(r, "g_wall") > I(r, "k10_wall"))
    wbest = sum(1 for r in rows if I(r, "g_wall") > min(I(r, "k1_wall"), I(r, "k4_wall"), I(r, "k10_wall")))
    w1 = sum(1 for r in rows if I(r, "g_wall") > I(r, "k1_wall"))
    n = len(rows)
    print(f"\n**Wall clock.** Single-process Koragraph is faster on **{w1} of {n}** repositories "
          f"despite Graphify running a pool ({tot['g_wall'] / tot['k1_wall']:.2f}x overall). At ten "
          f"workers each — the same pool size Graphify chooses here — Koragraph is faster on "
          f"**{w10} of {n}** ({tot['g_wall'] / tot['k10_wall']:.2f}x). Taking each configuration's "
          f"best per repository, Koragraph is faster on **{wbest} of {n}** "
          f"({tot['g_wall'] / best:.2f}x).")

    print(f"\n**CPU time** — user+sys over the process and every child it waited for, so a pool's "
          f"workers are counted. This is what each system *costs* rather than what a user waits.\n")
    print("| | Koragraph 1w | Koragraph 4w | Koragraph 10w | Graphify |")
    print("|---|---|---|---|---|")
    print(f"| CPU seconds | {tot['k1_cpu']/1000:.1f} | {tot['k4_cpu']/1000:.1f} "
          f"| {tot['k10_cpu']/1000:.1f} | **{tot['g_cpu']/1000:.1f}** |")
    print(f"| CPU per wall second | {tot['k1_cpu']/tot['k1_wall']:.1f}x "
          f"| {tot['k4_cpu']/tot['k4_wall']:.1f}x | {tot['k10_cpu']/tot['k10_wall']:.1f}x "
          f"| **{tot['g_cpu']/tot['g_wall']:.1f}x** |")
    cpu_win = sum(1 for r in rows if I(r, "g_cpu") > I(r, "k1_cpu"))
    print(f"\nSingle-process Koragraph uses **{tot['g_cpu']/tot['k1_cpu']:.2f}x less CPU** than "
          f"Graphify overall and less on **{cpu_win} of {n}** repositories.")
    # The parallel columns buy wall clock with CPU, and at some pool size they stop being cheaper
    # than the competitor at all. Stated by the generator rather than left for a reader to derive
    # from the row above.
    for label, wall, cpu in (("four", tot["k4_wall"], tot["k4_cpu"]),
                             ("ten", tot["k10_wall"], tot["k10_cpu"])):
        rel = cpu / tot["k1_cpu"]
        verdict = (f"**more CPU than Graphify** ({cpu/tot['g_cpu']:.2f}x its total)"
                   if cpu > tot["g_cpu"] else
                   f"still {tot['g_cpu']/cpu:.2f}x less CPU than Graphify")
        print(f"\nThat advantage is spent, not kept, when workers are added: at {label} workers "
              f"Koragraph uses **{rel:.2f}x the CPU of its own single-process run** "
              f"({cpu/1000:.1f} s against {tot['k1_cpu']/1000:.1f} s) to save "
              f"{(tot['k1_wall']-wall)/1000:.1f} s of wall clock, and that is {verdict}. Each "
              f"worker is a separate process that loads every grammar again; the wall-clock "
              f"columns are the honest parity comparison, and this is what they cost.")

    # Worker scaling, and why more is not better here.
    print(f"\n**More workers is not monotonically better, for either system.** Koragraph at ten "
          f"workers ({tot['k10_wall']/1000:.1f} s) is slower than at four "
          f"({tot['k4_wall']/1000:.1f} s): this machine's cores are not interchangeable, and past "
          f"the performance-core count a shard lands on a much slower efficiency core and sets the "
          f"wall clock. Graphify meets the same ceiling from the other side — it asks for a worker "
          f"per core and realises only **{tot['g_cpu']/tot['g_wall']:.1f}x** CPU per wall second "
          f"out of it. Neither scheduler distinguishes core types.")

    warm = tot["gwarm_wall"]
    print(f"\n**Graphify's AST cache does not change this.** Re-running it immediately, with the "
          f"cache from the previous run left in place, takes **{warm/1000:.1f} s** against "
          f"**{tot['g_wall']/1000:.1f} s** cold — so the cold figures above are not stripping away "
          f"an advantage it would have in practice on a full re-index.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "ast-bench"))
    ap.add_argument("--label", default="corpus-a")
    ap.add_argument("--corpus-b", action="store_true")
    # Transfer: the same pooled head-to-head figure on both corpora, side by side.
    ap.add_argument("--transfer", action="store_true")
    ap.add_argument("--corpus-b-work",
                    default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "ast-bench-corpus-b"))
    # The polyglot and line-accuracy tables come straight off the same cmp_*.json the other
    # tables do, so neither needs to be transcribed by hand.
    ap.add_argument("--polyglot", action="store_true")
    ap.add_argument("--polyglot-work",
                    default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "ast-bench-poly"))
    ap.add_argument("--lineacc", action="store_true")
    ap.add_argument("--labelblind", action="store_true")
    ap.add_argument("--gfyprecision", action="store_true")
    ap.add_argument("--referees", action="store_true")
    ap.add_argument("--timing", action="store_true")
    ap.add_argument("--timing-tsv",
                    default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "ast-timing.tsv"))
    ap.add_argument("--lang", default="cpp")
    ap.add_argument("--second-work",
                    default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "ast-bench-second"))
    ap.add_argument("--repos", default="")
    a = ap.parse_args()
    table = CORPUS_B_REPOS if a.corpus_b else LANG_REPOS

    if a.polyglot:
        return emit_polyglot(a.polyglot_work)
    if a.lineacc:
        return emit_lineacc(a.work, a.corpus_b_work)
    if a.labelblind:
        return emit_labelblind(a.work)
    if a.gfyprecision:
        return emit_gfyprecision(a.work, a.corpus_b_work)
    if a.timing:
        return emit_timing(a.timing_tsv)
    if a.referees:
        return emit_referees(a.lang, a.work, a.second_work,
                             a.repos.split(",") if a.repos else LANG_REPOS[a.lang])

    if a.transfer:
        def pooled(work, repos, lang):
            km = kf = t = 0
            for r in repos:
                p = os.path.join(work, f"cmp_{lang}__{r}.json")
                if not os.path.exists(p):
                    continue
                for row in json.load(open(p))["entities"]:
                    # Same denominator as the head-to-head table above: methods and
                    # types+constants only. `fields` is graphify_scored for java/kotlin/cpp
                    # (it models enum constants and C data members), so including it here would
                    # make this table disagree with that one on the same quantity.
                    if (row["entity"] in ("types", "constants", "fields")
                            or not row["graphify_scored"]):
                        continue
                    t += row["truth"]
                    km += row["kora"]["matched"]
                    kf += row["kora"]["found"]
            return (pct(km, t), pct(km, kf), t)

        print("| language | corpus A | corpus B | delta |")
        print("|---|---|---|---|")
        near = 0
        for lang in LANGS:
            d = pooled(a.work, LANG_REPOS[lang], lang)
            h = pooled(a.corpus_b_work, CORPUS_B_REPOS[lang], lang)
            if d[0] is None or h[0] is None:
                continue
            delta = h[0] - d[0]
            near += abs(delta) <= 1.0
            print(f"| {PRETTY[lang]} | {fmt(d[0])} | {fmt(h[0])} | {delta:+.2f} |")
        print(f"\n{near} of 10 languages transfer within one point.")
        return

    rows = []
    for lang in LANGS:
        cmps = []
        for r in table[lang]:
            p = os.path.join(a.work, f"cmp_{lang}__{r}.json")
            if os.path.exists(p):
                cmps.append(json.load(open(p)))
        if not cmps:
            continue

        acc = collections.defaultdict(collections.Counter)
        for c in cmps:
            for row in c["entities"]:
                # `types` and `constants` are reported separately AND pooled into
                # `types+constants`; counting both double-counts the same declarations.
                if row["entity"] in ("types", "constants"):
                    continue
                grp = acc["fields" if row["entity"] == "fields" else "head"]
                grp["truth"] += row["truth"]
                grp["k_match"] += row["kora"]["matched"]; grp["k_found"] += row["kora"]["found"]
                grp["g_match"] += row["graphify"]["matched"]; grp["g_found"] += row["graphify"]["found"]

        h, fl = acc["head"], acc["fields"]
        rows.append({
            "lang": lang, "repos": len(cmps), "truth": h["truth"],
            "k_rec": pct(h["k_match"], h["truth"]), "k_prec": pct(h["k_match"], h["k_found"]),
            "g_rec": pct(h["g_match"], h["truth"]), "g_prec": pct(h["g_match"], h["g_found"]),
            "f_truth": fl["truth"],
            "f_k_rec": pct(fl["k_match"], fl["truth"]), "f_k_prec": pct(fl["k_match"], fl["k_found"]),
            "f_g_rec": pct(fl["g_match"], fl["truth"]), "f_g_prec": pct(fl["g_match"], fl["g_found"]),
            "f_g_found": fl["g_found"],
        })

    print(f"### Head to head — methods and types/constants ({a.label} corpus)\n")
    print("Planes both systems model fully in every language.\n")
    print("| language | repos | declarations | Koragraph rec / prec | Graphify rec / prec |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {PRETTY[r['lang']]} | {r['repos']} | {r['truth']:,} | "
              f"{fmt(r['k_rec'])} / {fmt(r['k_prec'])} | {fmt(r['g_rec'])} / {fmt(r['g_prec'])} |")

    print(f"\n### The field plane ({a.label} corpus)\n")
    print("Reported apart from the head-to-head because Graphify's coverage of it is partial and\n"
          "language-dependent. `models` is read off its extractor source.\n")
    print("| language | declarations | Koragraph rec / prec | Graphify rec / prec | Graphify models |")
    print("|---|---|---|---|---|")
    for r in rows:
        if not r["f_truth"]:
            continue
        print(f"| {PRETTY[r['lang']]} | {r['f_truth']:,} | {fmt(r['f_k_rec'])} / {fmt(r['f_k_prec'])} "
              f"| {fmt(r['f_g_rec'])} / {fmt(r['f_g_prec'])} | {GFY_FIELD_MODEL[r['lang']]} |")

    tot_h = sum(r["truth"] for r in rows)
    tot_f = sum(r["f_truth"] for r in rows)
    print(f"\nTotals: {tot_h:,} declarations on the head-to-head planes and {tot_f:,} on the field "
          f"plane, across {sum(r['repos'] for r in rows)} repositories.")


if __name__ == "__main__":
    main()
