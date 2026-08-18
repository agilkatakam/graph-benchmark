#!/usr/bin/env python3
"""Score retrieved contexts against gold answers — blind to which system produced them.

BLINDNESS
---------
`score_context()` receives only (context_text, gold). It has no system argument, no filename, and
no way to tell one system's output from another's. Records are pooled, shuffled under a fixed
seed, scored, and only then re-joined to their system, so a scoring bug cannot preferentially
favour one side.

WHAT COUNTS AS A HIT
--------------------
Two metrics, both applied identically to every system, both reported:

  symbol      the gold declaration's name appears in the context as a whole word. System-agnostic:
              it does not care whether the context renders `NODE foo()`, `[METHOD] foo` or
              `foo(a, b) -> c`. This is the primary metric.

  symbol+file the name appears AND the context attributes it to the correct file on the same
              line. This is what a consuming model needs in order to cite a location.

A gold symbol is matched on its final component (`Session.get` -> `get`, `Foo::bar` -> `bar`) as
well as its full qualified form, because systems name declarations differently and demanding one
convention would score the convention rather than the retrieval. Short names can match
incidentally, so `--min-len` reruns the identical scoring over only those gold symbols whose
final component is at least N characters; both numbers are reported.

Usage:
  score.py --questions-dir <dir> --systems name=<dir> [name=<dir> ...] --out <results.json>
"""
import argparse
import glob
import json
import os
import random
import re
import sys

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    TOKENIZER = "tiktoken/cl100k_base/" + tiktoken.__version__
except Exception as exc:  # noqa: BLE001
    raise SystemExit(f"FATAL: tiktoken required for measured token counts: {exc}")

SHUFFLE_SEED = 20260817


def count_tokens(text):
    return len(_ENC.encode(text, disallowed_special=()))


def symbol_variants(symbol):
    """Full qualified name plus the final component, deduped and ordered longest-first."""
    parts = [p for p in re.split(r"::|\.|->", symbol) if p]
    out = [symbol]
    if parts and parts[-1] != symbol:
        out.append(parts[-1])
    return sorted(set(out), key=len, reverse=True)


def file_variants(path):
    """The repo-relative path and its basename. Systems render paths differently (leading ./,
    absolute prefixes, backslashes), so both forms are accepted for both systems."""
    base = os.path.basename(path)
    return sorted({path, path.replace("/", os.sep), base}, key=len, reverse=True)


IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def symbol_sequence(context):
    """Distinct identifier-like tokens in reading order.

    Used only to count how many distinct identifiers a context puts in front of the reader, which
    is format-neutral: no system's line discipline can move it.
    """
    seen, order = set(), []
    for tok in IDENT_RE.findall(context or ""):
        low = tok.lower()
        if low not in seen:
            seen.add(low)
            order.append(low)
    return order


def score_context(context, gold):
    """The blind scorer. No system identity is available here, by construction."""
    if not context or not context.strip():
        raise ValueError("empty context — refusing to score (a component failed silently)")

    lines = context.split("\n")
    lowered_lines = [l.lower() for l in lines]
    seq = symbol_sequence(context)
    hits = []
    for g in gold:
        sym_hit = False
        symfile_hit = False
        first_line = None
        for variant in symbol_variants(g["symbol"]):
            pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(variant) + r"(?![A-Za-z0-9_])")
            for i, line in enumerate(lines):
                if pat.search(line):
                    if not sym_hit:
                        sym_hit = True
                        first_line = i
                    low = lowered_lines[i]
                    if any(fv.lower() in low for fv in file_variants(g["file"])):
                        symfile_hit = True
                        first_line = min(first_line, i)
                        break
            if symfile_hit:
                break
        hits.append({"symbol": g["symbol"], "file": g["file"],
                     "symbol_hit": sym_hit, "symbol_file_hit": symfile_hit})
    # symbols_named: how many DISTINCT identifiers the context puts in front of the reader.
    # Without it, recall alone cannot distinguish "retrieved the right symbols" from "listed more
    # symbols" — and a format change that simply enumerates more candidates would read as a win.
    # Precision here is gold-hits / symbols-named: deliberately tiny for both systems (a context
    # legitimately names many non-gold symbols), so it is a RATIO to compare across systems at
    # equal recall, not a quality score in its own right.
    return {"hits": hits, "symbols_named": len(seq)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions-dir", required=True)
    ap.add_argument("--systems", nargs="+", required=True, help="name=<artifact dir>")
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-len", type=int, default=6)
    args = ap.parse_args()

    systems = {}
    for spec in args.systems:
        name, _, d = spec.partition("=")
        if not d:
            raise SystemExit(f"FATAL: --systems expects name=<dir>, got {spec!r}")
        systems[name] = d

    # ── Build the pooled, anonymised work list ────────────────────────────────
    # Each record carries an opaque key. The scorer sees only context + gold.
    records, meta = [], {}
    for qfile in sorted(glob.glob(os.path.join(args.questions_dir, "*.json"))):
        qs = json.load(open(qfile))
        gold_by_id = {q["id"]: q for q in qs["questions"]}
        stem = os.path.basename(qfile)[:-5]
        for sysname, sysdir in systems.items():
            path = os.path.join(sysdir, stem + ".json")
            if not os.path.exists(path):
                sys.stderr.write(f"[skip] no {sysname} artifact for {stem}\n")
                continue
            art = json.load(open(path))
            for r in art["results"]:
                q = gold_by_id[r["id"]]
                for budget, payload in r["budgets"].items():
                    key = f"{len(records):08d}"
                    meta[key] = {"system": sysname, "language": qs["language"], "repo": qs["repo"],
                                 "qid": r["id"], "budget": int(budget),
                                 "leaks_symbol": q["leaks_symbol"],
                                 "wall_s": r.get("wall_s"), "cpu_s": r.get("cpu_s")}
                    records.append({"key": key, "context": payload["context"], "gold": q["gold"]})

    if not records:
        raise SystemExit("FATAL: no records to score — refusing to emit an empty result")

    random.Random(SHUFFLE_SEED).shuffle(records)

    scored = {}
    for rec in records:
        res = score_context(rec["context"], rec["gold"])
        hits, symbols_named = res["hits"], res["symbols_named"]
        scored[rec["key"]] = {
            "hits": hits,
            "symbols_named": symbols_named,
            "chars": len(rec["context"]),
            "tokens": count_tokens(rec["context"]),
        }

    # ── Re-join and aggregate ─────────────────────────────────────────────────
    rows = []
    for key, s in scored.items():
        m = meta[key]
        gold_n = len(s["hits"])
        long_hits = [h for h in s["hits"] if len(re.split(r"::|\.|->", h["symbol"])[-1]) >= args.min_len]
        rows.append({
            **m,
            "gold_n": gold_n,
            "symbol_hits": sum(1 for h in s["hits"] if h["symbol_hit"]),
            "symbol_file_hits": sum(1 for h in s["hits"] if h["symbol_file_hit"]),
            "gold_n_long": len(long_hits),
            "symbol_hits_long": sum(1 for h in long_hits if h["symbol_hit"]),
            "recall": sum(1 for h in s["hits"] if h["symbol_hit"]) / gold_n if gold_n else 0.0,
            "recall_file": sum(1 for h in s["hits"] if h["symbol_file_hit"]) / gold_n if gold_n else 0.0,
            "chars": s["chars"], "tokens": s["tokens"],
            "symbols_named": s["symbols_named"],
            "precision": (sum(1 for h in s["hits"] if h["symbol_hit"]) / s["symbols_named"]
                          if s["symbols_named"] else 0.0),
        })

    out = {
        "tokenizer": TOKENIZER,
        "shuffle_seed": SHUFFLE_SEED,
        "min_len": args.min_len,
        "systems": list(systems),
        "blind": "score_context(context, gold) receives no system identity; records pooled and "
                 "shuffled before scoring, re-joined after",
        "rows": rows,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w"), indent=2)
    sys.stderr.write(f"scored {len(rows)} rows across {len(systems)} systems -> {args.out}\n")


if __name__ == "__main__":
    main()
