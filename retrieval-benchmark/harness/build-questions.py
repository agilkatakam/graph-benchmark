#!/usr/bin/env python3
"""Derive retrieval questions and gold answer sets from a repository's own git history.

WHY THIS EXISTS, AND WHY IT IS BUILT THIS WAY
---------------------------------------------
The question set IS the experiment. Phase 1's worst defect class was building a referee out of
our own extractor; the retrieval equivalent would be writing questions by looking at either
system's output. So no question here is authored by us.

  question text  = the maintainer's own commit subject, verbatim after mechanical cleaning.
                   Written years before this benchmark by people who had never heard of either
                   system.
  gold answers   = the declarations that commit actually modified, located by Universal Ctags —
                   a third-party indexer that neither Koragraph nor Graphify uses.

A stranger reproduces every gold answer with: the public repo, the public SHA, and `ctags`.

LINE NUMBERS ARE RESOLVED AT THE COMMIT, NAMES ARE CHECKED AT THE PIN
--------------------------------------------------------------------
A commit's hunk line numbers are line numbers *as of that commit*. Later commits shift them, so
mapping them onto the pinned checkout's declaration ranges silently mis-attributes. Instead the
file is indexed at its own revision (`git show <sha>:<path>`) to turn changed lines into
declaration *names*, and the name is then required to still exist at the pinned SHA. Names
survive line shifts; line numbers do not.

SYMBOL LEAKAGE IS MEASURED, NOT ASSUMED AWAY
--------------------------------------------
Some commit subjects name the symbol they changed ("fix NPE in SqlSessionFactoryBuilder.build").
Those questions reduce to lexical matching, which favours a lexical retriever. They are kept —
removing them would be us curating the set — but flagged, so results can be reported split by
`leaks_symbol` and neither side can be accused of benefiting from an uncontrolled variable.

Usage:
  build-questions.py --repo <checkout> --lang <lang> --name <repo-name> --out <path.json>
                     [--target 25] [--scan 4000]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

CTAGS_VERSION_REQUIRED = "Universal Ctags"

# Source extensions per language. A commit is only considered if it touches these.
LANG_EXTS = {
    "java":       (".java",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".mjs", ".cjs"),
    "python":     (".py",),
    "csharp":     (".cs",),
    "sql":        (".sql",),
    "cpp":        (".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh"),
    "go":         (".go",),
    "php":        (".php",),
    "kotlin":     (".kt", ".kts"),
}

# Applied identically to every language and republished with the results. A path matching any of
# these is not a source-of-behaviour file, so a commit touching only these yields no question.
NON_SOURCE_RE = re.compile(
    r"(^|/)(test|tests|spec|specs|__tests__|testdata|fixtures|examples?|samples?|"
    r"docs?|doc|website|benchmarks?|vendor|third_party|node_modules)(/|$)"
    r"|(^|/)[^/]*[._-](test|spec)s?\.[^/]+$"
    r"|(^|/)test_[^/]+$",
    re.IGNORECASE,
)

# Commit subjects that describe no behaviour. Mechanical, not editorial.
NOISE_SUBJECT_RE = re.compile(
    r"^\s*(merge\b|revert\b|bump\b|release\b|version\b|prepare\b|"
    r"\[maven-release-plugin\]|update (changelog|copyright|license|readme|deps|dependencies)\b|"
    r"chore\(deps\)|build\(deps\))",
    re.IGNORECASE,
)

# PROCESS commits: subjects that describe the development workflow rather than any behaviour of
# the code. "apply review comments" names no symbol, no feature and no bug, so there is nothing in
# it for a retrieval system to match on.
#
# CORRECTION (2026-08-18): an earlier version of this comment claimed "both systems scored 0.00 on
# every one of these". That was asserted, not measured, and it is FALSE. Of the three questions
# this rule actually removes from the shipped sets, two scored 1.000 for BOTH systems:
#   viper    "style: apply suggestions from code review"  -> 0.000 / 0.000
#   requests "Fix typo in documentation for verify"       -> 1.000 / 1.000
#   mybatis  "Fix typo in Arg Javadoc"                    -> 1.000 / (no artifact)
# The rule is kept because a workflow subject is not a retrieval question, which is the same
# ground as the merge/bump/release rule above — but it is NOT score-neutral, it was added after
# three scoring rounds existed, and both facts belong in the paper's disclosure section.
#
# The test is on the SUBJECT ALONE — it never reads a score, a gold set, or either system's
# output — and is applied uniformly to every language and repository.
PROCESS_SUBJECT_RE = re.compile(
    r"^\s*(apply|address|incorporate|implement)\s+(review|reviewer|pr|code[- ]review|feedback|"
    r"comments?|suggestions?)\b"
    r"|^\s*(review\s+)?(comments?|feedback|suggestions?)\s*$"
    r"|^\s*(fix(ed|es)?\s+)?typos?\b"
    r"|^\s*(code\s+)?(cleanup|clean\s*up|formatting|reformat|gofmt|rustfmt|lint(ing)?|style)\b"
    r"|^\s*(minor|small|misc(ellaneous)?|various)\s+(fix(es)?|change(s)?|update(s)?|cleanup)\b"
    r"|^\s*(wip|nit|nits|rebase|squash|amend)\b"
    r"|^\s*(add|update)\s+(comments?|javadoc|docstring|documentation)\s*$",
    re.IGNORECASE,
)

# Conventional-commit prefix and trailing issue refs. Stripping these is mechanical cleaning, not
# rewriting: the maintainer's words survive unchanged.
PREFIX_RE = re.compile(r"^\s*(fix|feat|feature|bug|bugfix|perf|refactor|chore|docs|style|test)"
                       r"(\([^)]*\))?\s*[:\-]\s*", re.IGNORECASE)
ISSUE_SUFFIX_RE = re.compile(r"\s*(\(#\d+\)|#\d+|\(GH-\d+\)|\[#\d+\])\s*$")


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed rc={p.returncode}: {p.stderr[:400]}")
    return p.stdout


def ctags_version():
    out = run(["ctags", "--version"], check=False)
    if CTAGS_VERSION_REQUIRED not in out:
        # Phase 1 lost an entire C/C++ result to BSD ctags emitting nothing and exiting 0.
        raise SystemExit(f"FATAL: need Universal Ctags, got: {out.splitlines()[0] if out else '<none>'}")
    line = out.splitlines()[0].strip()
    # Version, not just vendor. Gold sets differ between ctags releases (kinds, `end` coverage),
    # so a stranger on 6.1 would silently build a DIFFERENT gold set and compare against our
    # numbers. The version is recorded in every question file; this makes the mismatch loud.
    import re as _re
    m = _re.search(r"Universal Ctags (\d+)\.(\d+)", line)
    want = (6, 2)
    if m and (int(m.group(1)), int(m.group(2))) != want:
        sys.stderr.write(f"WARNING: gold sets were built with Universal Ctags {want[0]}.{want[1]}.x; "
                         f"you have {m.group(1)}.{m.group(2)}.x — regenerated questions may differ.\n")
    return line


def ctags_decls(content, ext):
    """Declarations in one file's content: [(name, kind, start, end)].

    --extras=+q emits both `Foo.bar` and `bar` for the same tag; the qualified form is kept and
    the bare duplicate at the same line/kind dropped, so a gold answer is never ambiguous.
    """
    with tempfile.NamedTemporaryFile("w", suffix=ext, delete=False, errors="replace") as fh:
        fh.write(content)
        tmp = fh.name
    try:
        out = run(["ctags", "--fields=+neKzS", "--extras=+q", "--output-format=json",
                   "-f", "-", tmp], check=False)
    finally:
        os.unlink(tmp)

    by_pos = {}
    ends = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if t.get("_type") != "tag" or "line" not in t:
            continue
        name, start = t["name"], int(t["line"])
        key = (start, t.get("kind", ""))
        # END must be recovered across BOTH variants of the same tag, not taken from whichever
        # one we happen to keep the name from. --extras=+q emits `Foo.bar` and `bar` for the same
        # declaration, and the QUALIFIED variant — the one preferred just below — is precisely the
        # one Universal Ctags emits without an `end` field. Taking its end alone fell back to
        # `end = start`, a zero-length span, so innermost() could never attribute a changed line to
        # the method and climbed to the enclosing class instead.
        #
        # Measured on commons-cli: shipped gold was 23 class / 4 interface / 4 field / 5 method —
        # for a Java repository — and 13 of 20 questions had the wrong gold. `end` coverage by
        # language: zlib 96.8%, requests 87.1%, viper 76.9%, commons-cli 52.4%, psr7 0.0%.
        if "end" in t:
            ends[key] = max(ends.get(key, start), int(t["end"]))
        ends.setdefault(key, start)
        prev = by_pos.get(key)
        # Prefer the qualified name; on a tie keep the longer (more specific) one.
        if prev is None or (("." in name or "::" in name) and "." not in prev[0] and "::" not in prev[0]) \
           or (len(name) > len(prev[0]) and ("." in name) == ("." in prev[0])):
            by_pos[key] = (name, t.get("kind", ""), start, start)
    return [(n, k, s, ends.get((s, k), s)) for (n, k, s, _e) in by_pos.values()]


def changed_lines(repo, sha, path):
    """Post-image line numbers changed by `sha` in `path`, from a zero-context diff."""
    out = run(["git", "-C", repo, "show", "-U0", "--no-color", "--format=", sha, "--", path],
              check=False)
    lines = set()
    for h in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", out, re.MULTILINE):
        start = int(h.group(1))
        count = int(h.group(2)) if h.group(2) else 1
        lines.update(range(start, start + count))
    return lines


def innermost(decls, line):
    """The tightest declaration spanning `line`, or None.

    Tightest, not outermost: a change inside a method attributes to the method, not to its class.
    Ties break on the later start so a nested declaration wins over its enclosing one.
    """
    best = None
    for name, kind, s, e in decls:
        if s <= line <= max(e, s):
            span = max(e, s) - s
            if best is None or span < best[0] or (span == best[0] and s > best[1]):
                best = (span, s, name, kind)
    return (best[2], best[3]) if best else None


def pinned_index(repo, exts):
    """(file, symbol) pairs present at the pinned checkout — the existence filter."""
    idx = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "vendor", "target", "build", "dist", "__pycache__"}]
        for f in files:
            if not f.endswith(exts):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, repo)
            try:
                with open(full, "r", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            idx[rel] = {d[0] for d in ctags_decls(content, os.path.splitext(f)[1])}
    return idx


def clean_subject(subject):
    s = PREFIX_RE.sub("", subject.strip())
    prev = None
    while prev != s:
        prev = s
        s = ISSUE_SUFFIX_RE.sub("", s).strip()
    return s.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--lang", required=True, choices=sorted(LANG_EXTS))
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", type=int, default=25)
    ap.add_argument("--scan", type=int, default=4000, help="commits to scan back from the pin")
    ap.add_argument("--min-subject", type=int, default=20)
    ap.add_argument("--max-gold", type=int, default=5)
    args = ap.parse_args()

    ctags_stamp = ctags_version()
    exts = LANG_EXTS[args.lang]
    pin = run(["git", "-C", args.repo, "rev-parse", "HEAD"]).strip()
    origin = run(["git", "-C", args.repo, "config", "--get", "remote.origin.url"], check=False).strip()

    sys.stderr.write(f"[{args.name}] pin={pin[:12]} indexing pinned checkout...\n")
    pinned = pinned_index(args.repo, exts)
    sys.stderr.write(f"[{args.name}] {len(pinned)} source files at pin\n")

    log = run(["git", "-C", args.repo, "log", "--no-merges", f"-n{args.scan}",
               "--format=%H%x1f%s", pin])

    questions, seen_subjects = [], set()
    stats = {"scanned": 0, "noise_subject": 0, "process_subject": 0, "short_subject": 0, "dup_subject": 0,
             "no_source_files": 0, "no_gold": 0, "too_many_gold": 0, "not_at_pin": 0, "kept": 0}

    for entry in log.split("\n"):
        if not entry.strip() or len(questions) >= args.target:
            continue
        sha, _, subject = entry.partition("\x1f")
        stats["scanned"] += 1

        if NOISE_SUBJECT_RE.search(subject):
            stats["noise_subject"] += 1
            continue
        if PROCESS_SUBJECT_RE.search(subject):
            stats["process_subject"] += 1
            continue
        text = clean_subject(subject)
        if len(text) < args.min_subject:
            stats["short_subject"] += 1
            continue
        norm = re.sub(r"\W+", " ", text.lower()).strip()
        if norm in seen_subjects:
            stats["dup_subject"] += 1
            continue

        files = [f for f in run(["git", "-C", args.repo, "show", "--name-only", "--format=",
                                 sha], check=False).split("\n") if f.strip()]
        src = [f for f in files if f.endswith(exts) and not NON_SOURCE_RE.search(f)]
        if not src:
            stats["no_source_files"] += 1
            continue

        gold = []
        for path in src:
            content = run(["git", "-C", args.repo, "show", f"{sha}:{path}"], check=False)
            if not content:
                continue
            decls = ctags_decls(content, os.path.splitext(path)[1])
            if not decls:
                continue
            hit = set()
            for ln in changed_lines(args.repo, sha, path):
                d = innermost(decls, ln)
                if d:
                    hit.add(d)
            for name, kind in sorted(hit):
                gold.append({"file": path, "symbol": name, "kind": kind})

        if not gold:
            stats["no_gold"] += 1
            continue
        if len(gold) > args.max_gold:
            stats["too_many_gold"] += 1
            continue
        # Every gold symbol must still exist at the pin, else neither system could return it.
        if not all(g["file"] in pinned and g["symbol"] in pinned[g["file"]] for g in gold):
            stats["not_at_pin"] += 1
            continue

        # Does the maintainer's own wording already name the answer? Measured, not filtered.
        low = text.lower()
        leaks = any(part.lower() in low
                    for g in gold
                    for part in re.split(r"[.:]{1,2}", g["symbol"]) if len(part) > 3)

        seen_subjects.add(norm)
        stats["kept"] += 1
        questions.append({
            "id": f"{args.lang}__{args.name}__{len(questions):04d}",
            "question": text,
            "leaks_symbol": leaks,
            "source": {"kind": "commit", "sha": sha, "subject": subject, "upstream": origin},
            "gold": [{**g, "locator": "universal-ctags", "locator_version": ctags_stamp}
                     for g in gold],
        })

    out = {
        "language": args.lang,
        "repo": args.name,
        "pinned_sha": pin,
        "upstream": origin,
        "locator": {"tool": "universal-ctags", "version": ctags_stamp,
                    "args": "--fields=+neKzS --extras=+q --output-format=json"},
        "filters": {
            "target": args.target, "scan_depth": args.scan, "min_subject_chars": args.min_subject,
            "max_gold_decls": args.max_gold, "no_merges": True,
            "non_source_pattern": NON_SOURCE_RE.pattern,
            "noise_subject_pattern": NOISE_SUBJECT_RE.pattern,
            "process_subject_pattern": PROCESS_SUBJECT_RE.pattern,
        },
        "selection_stats": stats,
        "questions": questions,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2)

    leak = sum(1 for q in questions if q["leaks_symbol"])
    sys.stderr.write(f"[{args.name}] kept {len(questions)} questions "
                     f"({leak} leak a symbol name) -> {args.out}\n")
    sys.stderr.write(f"[{args.name}] {json.dumps(stats)}\n")
    if not questions:
        raise SystemExit(f"FATAL: {args.name} yielded zero questions — refusing to emit an empty set")


if __name__ == "__main__":
    main()
