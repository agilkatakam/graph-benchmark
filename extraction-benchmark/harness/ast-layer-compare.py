#!/usr/bin/env python
"""Score the Koragraph and Graphify AST layers against the same tree-sitter ground truth.

Neither side is graded by its own definition of a declaration: the referee is tree-sitter's
Java grammar, which is what Graphify itself parses with.

Matching is by (file, name) multiset, so an overload that one side collapses shows up as a
recall miss rather than being silently forgiven. Line numbers are compared separately, with a
tolerance, because the two tools anchor a declaration differently (annotations vs signature).

Usage:
  python ast-layer-compare.py --truth truth.json --kora kora.json --graphify graph.json \
      --repo-name spring-petclinic-rest
"""
import argparse
import collections
import json
import os
import re


def multiset(pairs):
    return collections.Counter(pairs)


def score(truth_ms, got_ms):
    """Return (matched, missed, extra) as multiset intersection sizes."""
    inter = truth_ms & got_ms
    matched = sum(inter.values())
    return matched, sum(truth_ms.values()) - matched, sum(got_ms.values()) - matched


def pct(a, b):
    return 0.0 if not b else round(100.0 * a / b, 1)


# Must mirror ast-dump.js's per-language skip set EXACTLY. The referee and the extractor dump
# both refuse to walk these directories; Graphify's baseline is built by running `graphify
# update` over the whole checkout, so without this filter every declaration it correctly
# extracted from a skipped directory would be scored as a fabrication.
_SKIP_BASE = {".git", "node_modules", "target", "build", "vendor", ".gradle", "out"}
LANG_SKIP = {
    "java": _SKIP_BASE,
    "typescript": _SKIP_BASE | {"dist", "coverage", ".next", "bazel-out", ".angular"},
    "javascript": _SKIP_BASE | {"dist", "coverage", ".next", "bazel-out", ".angular"},
    "csharp": _SKIP_BASE | {"bin", "obj", "packages", "artifacts"},
    "python": _SKIP_BASE | {"__pycache__", ".tox", ".venv", "venv", "site-packages"},
    "cpp": _SKIP_BASE | {"deps", "third_party", "thirdparty", "cmake-build-debug", ".deps"},
    "sql": _SKIP_BASE | {"dist", "coverage", "__pycache__"},
    "go": _SKIP_BASE,
    "php": _SKIP_BASE,
    "kotlin": _SKIP_BASE,
}


# Kotlin allows any declaration to be named with backticks — `fun \`returns true\`()` is the
# standard spelling of a test name. The backticks are QUOTING SYNTAX, not part of the
# identifier: the compiler reports the inner text and the taxonomy follows it. Systems differ on
# whether they keep them, so stripping must be applied to truth and to every system alike —
# otherwise one declaration spelled two ways scores as a miss AND a fabrication. Same rule as
# norm_sql and norm_cpp.
def norm_kotlin(name):
    if isinstance(name, str) and "`" in name:
        return name.replace("`", "")
    return name


LANG_EXTS = {"java": (".java",), "typescript": (".ts", ".tsx", ".mts", ".cts"),
             "javascript": (".js", ".jsx", ".mjs", ".cjs"),
             "python": (".py", ".pyi"), "csharp": (".cs",), "sql": (".sql",),
             "cpp": (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
             "go": (".go",), "php": (".php",),
             "kotlin": (".kt", ".kts")}


# SQL identifiers are case-insensitive unless quoted, and the three systems qualify them
# differently: ScriptDom yields `sp_Blitz` for `[dbo].[sp_Blitz]`, Graphify's label is the
# whole `dbo.sp_Blitz`, and the scanner drops the schema but keeps the case. Matching on the
# raw string would score identical declarations as misses on every side. Applied to truth,
# Koragraph and Graphify alike — never to one of them.
def norm_sql(name):
    if not name:
        return name
    parts, buf, q = [], "", None
    for ch in str(name).strip():
        if q:
            if ch == q or (q == "[" and ch == "]"):
                q = None
            else:
                buf += ch
            continue
        if ch in '"`[':
            q = ch
            continue
        if ch == ".":
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    parts.append(buf)
    name = parts[-1].strip().lower()
    # PostgreSQL truncates an identifier at NAMEDATALEN-1 = 63 bytes, so a 79-character table
    # name in the source really is a 63-character object in the database. libpg_query reports
    # the truncated name and a text scanner reports what is written, so truncating on every side
    # makes the two spellings the same object rather than one miss plus one fabrication.
    return name[:63] or None


# Universal Ctags spells an operator `operator =`, tree-sitter's `operator_name` node spells it
# `operator=`. Whitespace inside the name is removed on every side so the two spellings are one
# declaration. The destructor tilde is NOT stripped: ctags keeps it (`~Foo`), and dropping it
# collapses a destructor onto its class's constructor, which manufactures a miss and a
# fabrication out of one correct extraction on every class that declares both.
# A template specialization is spelled with its argument list by Graphify
# (`BuiltInDefaultValue<const T>`) and bare by both ctags and Koragraph, so the argument list is
# stripped on every side to make the two spellings one declaration.
_CPP_TMPL = re.compile(r"<[^<>]*(?:<[^<>]*>[^<>]*)*>")


def norm_cpp(name):
    if not isinstance(name, str):
        return name
    if name.startswith("operator"):
        return "".join(name.split())
    stripped = _CPP_TMPL.sub("", name).strip()
    # `void parse_context::do_check_arg_id(...)` is an out-of-class definition of a member.
    # Graphify labels it with the qualification, ctags and Koragraph with the bare member name.
    # Dropping the qualifier on every side is the same normalisation `norm_sql` applies to a
    # schema-qualified object. `operator` names are returned above, before this runs, so
    # `operator::` — which is not valid C++ anyway — cannot be reached.
    if "::" in stripped:
        stripped = stripped.rsplit("::", 1)[-1].strip()
    return stripped or name


def load_graphify(path, repo_root_hint, lang="java", checkout=None):
    d = json.load(open(path))
    exts = LANG_EXTS[lang]
    skip = LANG_SKIP.get(lang, _SKIP_BASE)

    def _in_skipped_dir(src):
        return any(part in skip for part in str(src).split(os.sep))

    nodes = [n for n in d["nodes"]
             if str(n.get("source_file", "")).endswith(exts)
             and not _in_skipped_dir(n.get("source_file", ""))]
    links = d.get("links", [])

    if lang in ("typescript", "javascript"):
        return _load_graphify_ts(d, nodes, links, repo_root_hint, lang, checkout)
    if lang == "sql":
        return _load_graphify_sql(d, nodes, links, repo_root_hint, checkout)
    if lang in ("python", "csharp", "cpp"):
        return _load_graphify_py(d, nodes, links, repo_root_hint,
                                 field_relations=((("defines", "field"),) if lang == "cpp" else ()),
                                 checkout=(checkout if lang == "cpp" else None))
    if lang in ("go", "php", "kotlin"):
        # These use the generic engine's label convention (engine.py:3209) — a method renders
        # `.name()`, a top-level function `name()`, a type bare. Read off Graphify's source, not
        # assumed: the Java-style inbound-edge encoding would file every top-level function as a
        # spurious type and understate Graphify's type precision.
        return _load_graphify_ts(d, nodes, links, repo_root_hint, lang, checkout)

    # Graphify stores no node type for Java. Role is implied by inbound edge relation:
    # `method` -> a method, `contains` from a file -> a declared type, L1 -> the file itself.
    inbound = collections.defaultdict(set)
    for l in links:
        inbound[l["target"]].add(l["relation"])

    methods, types, fields, files = [], [], [], []
    nodes, aux = _split_by_file_type(nodes)
    for n in nodes:
        nid = n["id"]
        rel = n["source_file"]
        # Normalise to a repo-relative path so it lines up with the referee's keys. Strip the
        # exact checkout prefix only — splitting on the bare repo name mangles nested layouts
        # (gson's own paths contain "gson" several times and are already relative).
        rel = _rel_path(rel, repo_root_hint)
        label = n.get("label", "")
        loc = n.get("source_location", "") or ""
        line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
        rels = inbound.get(nid, set())
        if label.endswith(".java"):
            files.append((rel, label))
        # Graphify labels a callable with no owning class `name()` with a `contains` edge rather
        # than `.name()` with a `method` edge — in Java that is every method inside an anonymous
        # class or enum body, so the label test must be applied here as in every other decoder or
        # those callables are filed as types.
        elif "case_of" in rels:
            fields.append({"file": rel, "name": label, "line": line})
        elif "method" in rels or label.startswith(".") or label.endswith("()"):
            methods.append({"file": rel, "name": label.lstrip(".").rstrip("()"), "line": line})
        else:
            types.append({"file": rel, "name": label, "line": line})
    return {"methods": methods, "types": types, "fields": fields, "files": files, "aux": aux,
            "links": links, "node_count": len(d["nodes"])}


# The corpus directory is not fixed: corpus B lives in `.corpus-b-checkouts`. Strip the
# exact checkout prefix only — splitting on the bare repo name mangles nested layouts (gson's
# own paths contain "gson" several times and are already relative).
_CORPUS_DIRS = (".corpus-a-checkouts", ".corpus-b-checkouts", ".polyglot-checkouts")


def _rel_path(rel, repo_root_hint):
    """Strip whichever corpus prefix applies, wherever the corpus lives.

    The list above is only a fast path for this repository's own layout; corpora cloned anywhere
    else are handled by the general rule below. Without that rule no path lines up with the
    referee's keys and every match is scored as a miss.
    """
    for base in _CORPUS_DIRS:
        prefix = f"{base}/{repo_root_hint}/"
        if rel.startswith(prefix):
            return rel[len(prefix):]
    # General case, for a reader whose corpora live somewhere else: drop everything up to and
    # including the checkout directory. Applied ONLY to an absolute path — an already-relative
    # `source_file` must never be touched. Graphify's paths are already relative, and a repo
    # whose own tree contains a directory named after itself (etcd/server/etcd, hugo/.../hugo)
    # would otherwise be truncated to a bare filename, manufacturing unmatchable entries.
    if os.path.isabs(rel):
        parts = rel.split(os.sep)
        if repo_root_hint in parts[:-1]:
            return os.sep.join(parts[len(parts) - parts[::-1].index(repo_root_hint):])
    return rel


# Graphify tags every node with a `file_type`. Its declaration nodes are `code`; `rationale`
# and `doc_ref` are its comment-derived planes, and `concept` is a clustering artefact. Scoring
# the non-code ones as declarations would count comment-derived nodes as spurious types, so they
# are counted separately instead — both systems emit them, so this is a comparison, not an
# exclusion.
def _split_by_file_type(nodes):
    decl, aux = [], collections.Counter()
    for n in nodes:
        ft = n.get("file_type")
        # A namespace/package/module node is a CONTAINER, not a declaration of a type. Graphify
        # models one and Koragraph does not, so bucketing them with types would charge Graphify
        # fabricated types for a capability it has and the other system lacks.
        if n.get("type") in ("namespace", "package", "module"):
            aux["container"] += 1
        elif ft in ("rationale", "doc_ref", "concept"):
            aux[ft] += 1
        else:
            decl.append(n)
    return decl, aux


def _load_graphify_ts(d, nodes, links, repo_root_hint, lang="typescript", checkout=None):
    """Graphify's TypeScript role encoding differs from its Java one and must be read on its
    own terms or the comparison is unfair to it.

    In Java, every callable carries an inbound `method` edge. In TypeScript only *class*
    methods do: a top-level `function` is `contains`-ed by its file exactly like a type is.
    The one signal that separates them across both cases is Graphify's own label convention —
    a callable is rendered `name()` (and `.name()` when it is a class member), a type is
    rendered bare. Classifying by the Java rule here would file every top-level function as a
    spurious type, which would understate their type precision and their method recall.
    """
    methods, types, fields, files = [], [], [], []
    nodes, aux = _split_by_file_type(nodes)
    src_cache = {}

    # A TypeScript `namespace`/`declare module` node, and the alias node `export * as a from
    # "./a"` creates, are CONTAINERS — the same species `_split_by_file_type` excludes for C#.
    # That rule keys on Graphify's `type` field, which its TS walk never sets
    # (engine.py:1866-1872 calls add_node with no node_type), so containers must be detected
    # from the source line here instead or they score as fabricated types.
    def is_container_decl(rel, line):
        if not checkout or line is None or lang not in ("typescript", "javascript"):
            return False
        if rel not in src_cache:
            try:
                with open(os.path.join(checkout, rel), encoding="utf8", errors="replace") as fh:
                    src_cache[rel] = fh.read().split("\n")
            except OSError:
                src_cache[rel] = []
        lines = src_cache[rel]
        if not 0 < line <= len(lines):
            return False
        head = lines[line - 1].lstrip()
        for kw in ("export ", "declare ", "default "):
            while head.startswith(kw):
                head = head[len(kw):].lstrip()
        return head.startswith(("namespace ", "module ", "* as "))

    inbound, outbound = collections.defaultdict(set), collections.defaultdict(set)
    for l in links:
        inbound[l["target"]].add(l["relation"])
        outbound[l["source"]].add(l["relation"])
    for n in nodes:
        rel = _rel_path(n["source_file"], repo_root_hint)
        label = n.get("label", "")
        loc = n.get("source_location", "") or ""
        line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
        rels = inbound.get(n["id"], set())
        # An enum entry. `case_of` routes to the field plane here exactly as it does in the Java
        # decoder; without it enum entries land in the TYPE plane and score as fabricated types.
        # Graphify does have a field plane — it holds enum entries and nothing else.
        if "case_of" in rels:
            fields.append({"file": rel, "name": label.lstrip("."), "line": line})
            continue
        # A receiver-owner stub. For `func (c *Command) Foo()` in a file that does not declare
        # `Command`, Graphify creates a `Command` node there to hang the `method` edge on. No
        # inbound edge, outbound `method` edges, no declaration behind it — the same species as
        # the C# `namespace` container, which `_split_by_file_type` excludes by its `type` field.
        # Go nodes carry no `type` field, so they must be detected structurally here instead.
        if (lang == "go" and not label.endswith("()") and not label.startswith(".")
                and not rels and "method" in outbound.get(n["id"], set())):
            aux["receiver_owner_stub"] += 1
            continue
        # The taxonomy excludes destructuring patterns and computed property names from truth
        # ("no stable identifier to match on"), so they must also be excluded from Graphify's
        # denominator — otherwise `const { Buffer } = require(...)` and `[Symbol.iterator]() {}`
        # are charged to it as fabrications. An exclusion applied to truth applies to every side.
        bare = label.lstrip(".")
        if bare.startswith("{") or bare.startswith("[") or bare.startswith("("):
            aux["excluded_by_taxonomy"] += 1
            continue
        if label.endswith(LANG_EXTS[lang]):
            files.append((rel, label))
        elif is_container_decl(rel, line):
            aux["container"] += 1
        elif label.endswith("()"):
            # A quoted key (`"Program:exit"(node) {}`) is the same declaration truth records
            # unquoted; keeping the quotes makes one declaration a miss AND a fabrication.
            methods.append({"file": rel, "name": bare[:-2].strip('"\''), "line": line})
        else:
            types.append({"file": rel, "name": bare.strip('"\''), "line": line})
    return {"methods": methods, "types": types, "fields": fields, "files": files, "aux": aux,
            "links": links, "node_count": len(d["nodes"])}


def _load_graphify_sql(d, nodes, links, repo_root_hint, checkout=None):
    """Graphify's SQL extractor labels a routine `name()` and everything else bare — the same
    convention as its TypeScript output, read off `extractors/sql.py` rather than assumed. Its
    node set is tables, views, functions, procedures and triggers; it emits no columns, no
    materialized views, no types, no domains and no sequences.

    Its `alter_table` branch (extractors/sql.py:146-153) creates a node for the ALTERED object
    when that object was not CREATEd in the same file, purely so the `references` edge it is
    about to write has an endpoint. That is the same species as the C# `namespace` node
    `_split_by_file_type` excludes: a CONTAINER, or here an edge endpoint, not a declaration —
    `ALTER TABLE x ADD CONSTRAINT` declares no table. Excluded whether or not they happen to
    match truth, so the rule is a definition rather than a post-hoc adjustment.
    """
    methods, types, files = [], [], []
    nodes, aux = _split_by_file_type(nodes)
    src_cache = {}

    def anchored_on_alter(rel, line):
        if not checkout or line is None:
            return False
        if rel not in src_cache:
            try:
                with open(os.path.join(checkout, rel), encoding="utf8", errors="replace") as fh:
                    src_cache[rel] = fh.read().split("\n")
            except OSError:
                src_cache[rel] = []
        lines = src_cache[rel]
        return 0 < line <= len(lines) and lines[line - 1].lstrip().upper().startswith("ALTER")

    for n in nodes:
        rel = _rel_path(n["source_file"], repo_root_hint)
        label = n.get("label", "")
        loc = n.get("source_location", "") or ""
        line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
        if label.lower().endswith(".sql"):
            files.append((rel, label))
        elif label.endswith("()"):
            methods.append({"file": rel, "name": label[:-2], "line": line})
        elif anchored_on_alter(rel, line):
            aux["alter_target"] += 1
        else:
            types.append({"file": rel, "name": label, "line": line})
    return {"methods": methods, "types": types, "files": files, "aux": aux,
            "links": links, "node_count": len(d["nodes"])}


# C and C++ declaration shapes that need the source line to adjudicate, because Graphify's
# label alone cannot distinguish them.
_CPP_FWD_DECL = re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?(?:struct|class|union|enum(?:\s+class)?)\s+"
                           r"(?:__attribute__\s*\(\([^)]*\)\)\s*|\w+_API\s+)*[\w:]+\s*;")
_CPP_NAMESPACE = re.compile(r"^\s*(?:inline\s+)?namespace\b")
_CPP_ACCESS_KEYWORDS = {"public", "private", "protected", "friend", "using", "template",
                        "typedef", "static", "virtual", "explicit", "inline", "constexpr"}
_CPP_FUNC_LINE = re.compile(r"[\w~]+\s*\([^;=]*\)\s*(?:const\s*)?(?:noexcept\s*)?"
                            r"(?:override\s*|final\s*)?(?:=\s*(?:0|default|delete)\s*)?;")


def _load_graphify_py(d, nodes, links, repo_root_hint, field_relations=(), checkout=None):
    """Graphify's Python output uses the Java-style encoding: a callable carries an inbound
    `method` edge, a class is `contains`-ed by its file. Verified against a baseline rather
    than assumed — the TypeScript encoding differs, so this cannot be taken for granted."""
    inbound = collections.defaultdict(set)
    inbound_ctx = collections.defaultdict(set)
    for l in links:
        inbound[l["target"]].add(l["relation"])
        inbound_ctx[l["target"]].add((l["relation"], l.get("context")))
    methods, types, fields, files = [], [], [], []
    nodes, aux = _split_by_file_type(nodes)
    src_cache = {}

    def src_line(rel, line, join=0):
        if not checkout or line is None:
            return None
        if rel not in src_cache:
            try:
                with open(os.path.join(checkout, rel), encoding="utf8", errors="replace") as fh:
                    src_cache[rel] = fh.read().split("\n")
            except OSError:
                src_cache[rel] = []
        lines = src_cache[rel]
        if not 0 < line <= len(lines):
            return None
        # A prototype whose parameter list wraps is still a prototype. Testing ONE physical line
        # files every wrapped one as a FIELD — a fabrication on the field plane and a miss on the
        # method plane at once — so the following lines are joined before the test.
        return " ".join(x.strip() for x in lines[line - 1: line + join])

    for n in nodes:
        rel = _rel_path(n["source_file"], repo_root_hint)
        label = n.get("label", "")
        loc = n.get("source_location", "") or ""
        line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
        rels = inbound.get(n["id"], set())
        if label.endswith((".py", ".pyi", ".cs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
                           ".hh", ".hxx")):
            files.append((rel, label))
        # C/C++ IS scored on fields: Graphify's engine.py emits a node per data member wired
        # `defines`/`field`, so those nodes belong on the field plane rather than the type plane.
        #
        # `struct redisDb;` is a FORWARD DECLARATION. It introduces a name and declares no type —
        # ctags does not tag it and truth has no entry for it, but Graphify creates a node so its
        # `contains` edge has an endpoint. Same species as the SQL `ALTER` target and the Go
        # receiver-owner stub, and it appears in headers.
        elif _CPP_FWD_DECL.match(src_line(rel, line) or ""):
            aux["forward_declaration"] += 1
        # A C++ `namespace` is a container, not a declaration of a type or a callable — the
        # same rule already applied to C# namespaces and TypeScript `declare module`. Graphify's
        # C++ walk emits one (labelled `namespace`, or the namespace's own name for a named or
        # inline one) and its `type` field is unset, so the generic container rule cannot fire.
        elif label == "namespace" or _CPP_NAMESPACE.match(src_line(rel, line) or ""):
            # `SPDLOG_NAMESPACE_BEGIN`, `FMT_BEGIN_NAMESPACE` and `NLOHMANN_JSON_NAMESPACE_BEGIN`
            # open a namespace through a macro, so the source line does not begin with the
            # keyword — but Graphify still labels the node `namespace`, so the label test above
            # catches those.
            aux["namespace"] += 1
        # Graphify's `field_declaration` branch computes `is_method` but uses it only to gate
        # reference edges — the node emit runs unconditionally, so `virtual int compute(int)
        # const;` gets a `defines`/`field` edge exactly like `int a;` does, and its C++ labels
        # carry no `()` to tell them apart. Filing all of them as fields would move member
        # function prototypes off the method plane, so they are adjudicated on the declaration
        # line, where the parameter list is. `public:`, `friend` and `using` declare nothing and
        # are dropped.
        elif field_relations and (inbound_ctx.get(n["id"], set()) & set(field_relations)):
            src = src_line(rel, line, join=3) or ""
            if label in _CPP_ACCESS_KEYWORDS:
                aux["cpp_keyword"] += 1
            elif label.rstrip().endswith("()") or _CPP_FUNC_LINE.search(src):
                methods.append({"file": rel, "name": label.lstrip(".").rstrip("()"), "line": line})
            else:
                fields.append({"file": rel, "name": label, "line": line})
        elif "method" in rels or label.startswith(".") or label.endswith("()"):
            methods.append({"file": rel, "name": label.lstrip(".").rstrip("()"), "line": line})
        else:
            types.append({"file": rel, "name": label, "line": line})
    return {"methods": methods, "types": types, "fields": fields, "files": files, "aux": aux,
            "links": links, "node_count": len(d["nodes"])}


def line_accuracy(truth_items, got_items, tol=2):
    """Of the entries both sides agree exist, how many agree on the line within tol."""
    tmap = collections.defaultdict(list)
    for t in truth_items:
        tmap[(t["file"], t["name"])].append(t["line"])
    ok = tot = 0
    for g in got_items:
        cands = tmap.get((g["file"], g["name"]))
        if not cands or g.get("line") is None:
            continue
        tot += 1
        if any(abs(g["line"] - c) <= tol for c in cands):
            ok += 1
    return ok, tot


# The primary referee per language, mirroring ast-bench.sh's dispatch. Stamped into every
# artifact so a reader knows which tool defined truth for that row without consulting the
# harness. --referee overrides it (ast-second-referee.sh passes the second referee's name).
PRIMARY_REFEREE = {
    "java": "tree-sitter-java",
    "typescript": "TypeScript compiler (typescript 5.x AST)",
    "javascript": "TypeScript compiler (typescript 5.x AST)",
    "python": "CPython ast",
    "csharp": "Roslyn",
    "sql": "pglast / ScriptDom / sqlglot, dialect-routed",
    "cpp": "Universal Ctags",
    "go": "go/parser",
    "php": "PHP ext/ast",
    "kotlin": "Kotlin compiler PSI",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", required=True)
    ap.add_argument("--kora", required=True)
    # Optional: a repo with no Graphify baseline is still scored against the referee.
    ap.add_argument("--graphify", default=None)
    ap.add_argument("--repo-name", required=True)
    ap.add_argument("--referee", default=None,
                    help="referee name to stamp; defaults to the primary referee for --lang")
    # The checkout path. Needed for two things the node JSON alone cannot answer: stripping the
    # right prefix off Graphify's absolute source_file (the corpus directory is not fixed), and
    # reading the statement a SQL node is anchored on.
    ap.add_argument("--checkout", default=None)
    ap.add_argument("--lang", default="java", choices=["java", "typescript", "javascript", "python", "csharp", "sql", "cpp", "go", "php", "kotlin"])
    a = ap.parse_args()

    truth = json.load(open(a.truth))
    kora = json.load(open(a.kora))
    gf = (load_graphify(a.graphify, a.repo_name, a.lang, a.checkout) if a.graphify
          else {"methods": [], "types": [], "files": [], "links": [], "node_count": None,
                "aux": collections.Counter()})

    t_methods = [{"file": m["file"], "name": m["name"], "line": m["line"]} for m in truth["methods"]]
    t_types = [{"file": t["file"], "name": t["name"], "line": t["line"]} for t in truth["types"]]
    t_fields = [{"file": f["file"], "name": f["name"], "line": f["line"]} for f in truth["fields"]]
    t_constants = [{"file": c["file"], "name": c["name"], "line": c["line"]}
                   for c in truth.get("constants", [])]

    # `.pyi` is not in Graphify's CODE_EXTENSIONS (graphify/detect.py:31), so it never opens a
    # stub file. Charging it recall for declarations it cannot reach is the skip-set asymmetry in
    # the other direction, so stubs are excluded from all three sides here and counted
    # separately — stub coverage belongs in the "additionally emitted" column, not inside a
    # head-to-head recall figure.
    pyi_excluded = 0
    if a.lang == "python":
        for plane in (t_methods, t_types, t_fields, t_constants):
            before = len(plane)
            plane[:] = [x for x in plane if not str(x["file"]).endswith(".pyi")]
            pyi_excluded += before - len(plane)

    k_nodes = kora["nodes"]
    if a.lang == "python":
        k_nodes = [n for n in k_nodes if not str(n["file"]).endswith(".pyi")]

    # A referee that parses one compilation configuration (Roslyn) cannot see declarations
    # inside `#if`-disabled regions. They are real declarations, but no referee here can
    # adjudicate them, and counting a system's recovery of them as false positives would score
    # the referee's blind spot. Excluded from BOTH sides when the truth file reports them.
    disabled = truth.get("disabled_ranges") or {}
    if disabled:
        def _in_disabled(n):
            for lo, hi in disabled.get(n["file"], ()):  # inclusive line range
                if n.get("line") is not None and lo <= n["line"] <= hi:
                    return True
            return False
        before = len(k_nodes)
        k_nodes = [n for n in k_nodes if not _in_disabled(n)]
        disabled_excluded = before - len(k_nodes)
        # Applied to BOTH sides and to truth. For C# it makes no difference — Roslyn cannot
        # produce truth inside a region it never parsed — but a SQL referee marks a whole file
        # unadjudicated when its parser errored anywhere in it while still reporting what it read
        # before the error, so those truth entries must be dropped too.
        for plane in (t_methods, t_types, t_fields, t_constants,
                      gf.get("methods", []), gf.get("types", []), gf.get("fields", [])):
            plane[:] = [n for n in plane if not _in_disabled(n)]
    else:
        disabled_excluded = 0
    # SQL's type plane is the named schema objects — the scanner stamps them with the product
    # node types, not with CLASS.
    SQL_TYPE_NODES = ("DB_TABLE", "DB_VIEW", "DB_TYPE", "DB_TRIGGER")

    k_methods = [{"file": n["file"], "name": n["name"], "line": n["line"]}
                 for n in k_nodes if n["node_type"] == "METHOD"]
    # Derived scopes (anonymous classes, enum-constant bodies) are modelled as unnamed by the
    # referee, so they are excluded from the type score rather than credited.
    k_types = [{"file": n["file"], "name": n["name"], "line": n["line"]}
               for n in k_nodes
               if n["node_type"] in (SQL_TYPE_NODES if a.lang == "sql"
                                     else ("CLASS", "ENTITY", "INTERFACE"))
               and n["kind"] not in ("anonymous_class", "enum_constant")]
    k_fields = [{"file": n["file"], "name": n["name"], "line": n["line"]}
                for n in k_nodes if n["node_type"] == "FIELD"]
    k_constants = [{"file": n["file"], "name": n["name"], "line": n["line"]}
                   for n in k_nodes if n["node_type"] == "CONSTANT"]

    if a.lang == "cpp":
        for group in (t_methods, t_types, t_fields, t_constants,
                      k_methods, k_types, k_fields, k_constants,
                      gf["methods"], gf["types"], gf.get("fields", [])):
            for item in group:
                item["name"] = norm_cpp(item["name"])

    if a.lang == "kotlin":
        for group in (t_methods, t_types, t_fields, t_constants,
                      k_methods, k_types, k_fields, k_constants,
                      gf["methods"], gf["types"], gf.get("fields", [])):
            for item in group:
                item["name"] = norm_kotlin(item["name"])

    if a.lang == "sql":
        for group in (t_methods, t_types, t_fields, t_constants,
                      k_methods, k_types, k_fields, k_constants,
                      gf["methods"], gf["types"]):
            for item in group:
                item["name"] = norm_sql(item["name"])
            group[:] = [item for item in group if item["name"]]

    # Truth carries a `kind` on every entry for TypeScript; report per-kind recall so a plane
    # that is 99% overall but 0% on, say, enum members cannot hide inside the aggregate.
    kind_rows = []
    if a.lang in ("typescript", "javascript", "python", "csharp", "sql", "cpp", "go", "php", "kotlin"):
        km_sets = {
            "types": multiset([(x["file"], x["name"]) for x in k_types]),
            "methods": multiset([(x["file"], x["name"]) for x in k_methods]),
            "fields": multiset([(x["file"], x["name"]) for x in k_fields]),
            "constants": multiset([(x["file"], x["name"]) for x in k_constants]),
        }
        for plane in ("types", "methods", "fields", "constants"):
            by_kind = collections.defaultdict(list)
            for item in truth.get(plane, []):
                if disabled and _in_disabled(item):
                    continue
                by_kind[item.get("kind") or "?"].append(item)
            for kind, items in sorted(by_kind.items()):
                tm = multiset([(x["file"], x["name"]) for x in items])
                matched = sum((tm & km_sets[plane]).values())
                kind_rows.append({"plane": plane, "kind": kind, "truth": sum(tm.values()),
                                  "kora_recall": pct(matched, sum(tm.values()))})

    # Graphify emits no node taxonomy: its label convention separates callables (`name()`) from
    # everything else, and for some languages that "everything else" bucket holds types AND
    # module constants together. Scoring it against types alone would count every constant it
    # emits as a spurious type, so the non-callable bucket is scored once against types+constants
    # — but ONLY for the languages whose Graphify extractor actually emits constant nodes.
    # Pooling constants into the denominator for a language whose extractor emits none adds rows
    # it cannot reach and understates its recall.
    #
    # Membership of GFY_MODELS_CONSTANTS is decided per language by the same probe, not asserted:
    # count how many of Graphify's non-callable nodes land on a truth TYPE versus a truth
    # CONSTANT, and pool only where the constant count is non-trivial. Re-run that probe before
    # adding a language here; for languages whose truth carries no constants plane at all the
    # question does not arise.
    GFY_MODELS_CONSTANTS = {"typescript", "javascript"}
    pool_constants = a.lang in GFY_MODELS_CONSTANTS
    t_noncallable = t_types + (t_constants if pool_constants else [])
    k_noncallable = k_types + (k_constants if pool_constants else [])
    # Graphify is scored on TWO rows only — its label convention splits its nodes into
    # callables and everything-else, and nothing finer exists to score. The other three rows
    # pass an empty list for it, which arithmetically yields 0.0% recall and 0.0% precision.
    # That zero is an absent input, not a result: every row carries `graphify_scored`, and the
    # bench prints a dash rather than a zero when it is false.
    rows = []
    for label, t, k, g, g_scored in [
        ("methods", t_methods, k_methods, gf["methods"], True),
        ("types", t_types, k_types, [], False),
        ("constants", t_constants, k_constants, [], False),
        ("types+constants", t_noncallable, k_noncallable, gf["types"], True),
        ("fields", t_fields, k_fields, gf.get("fields", []), bool(gf.get("fields"))),
    ]:
        tm = multiset([(x["file"], x["name"]) for x in t])
        km = multiset([(x["file"], x["name"]) for x in k])
        gm = multiset([(x["file"], x["name"]) for x in g])
        k_match, k_miss, k_extra = score(tm, km)
        g_match, g_miss, g_extra = score(tm, gm)
        rows.append({
            "entity": label, "truth": sum(tm.values()), "graphify_scored": g_scored,
            "kora": {"found": sum(km.values()), "matched": k_match, "missed": k_miss,
                     "extra": k_extra, "recall": pct(k_match, sum(tm.values())),
                     "precision": pct(k_match, sum(km.values()))},
            "graphify": {"found": sum(gm.values()), "matched": g_match, "missed": g_miss,
                         "extra": g_extra, "recall": pct(g_match, sum(tm.values())),
                         "precision": pct(g_match, sum(gm.values()))},
        })

    # Whether the competitor emits fields is answered from its own nodes rather than inferred
    # from the unscored row above: how many of its nodes, of ANY label shape, land on a
    # (file, name) the referee calls a field.
    gf_all = multiset([(x["file"], x["name"]) for x in gf["methods"] + gf["types"]])
    gf_field_overlap = multiset([(x["file"], x["name"]) for x in t_fields]) & gf_all
    gf_field_hits = sum(gf_field_overlap.values())
    # ...and how many of those are a name the referee ALSO calls a method or a type in the same
    # file. Those are collisions, not evidence of a field plane, so both counts are emitted and
    # the ratio is read from the artifact rather than restated.
    t_mt = multiset([(x["file"], x["name"]) for x in t_methods + t_types])
    gf_field_collisions = sum((gf_field_overlap & t_mt).values())

    k_ok, k_tot = line_accuracy(t_methods, k_methods)
    g_ok, g_tot = line_accuracy(t_methods, gf["methods"])

    spans_k = sum(1 for n in k_nodes if n.get("end_line"))
    calls_k = sum(n["calls"] for n in k_nodes)
    calls_recv = sum(n["calls_with_receiver"] for n in k_nodes)

    print(json.dumps({
        "repo": a.repo_name,
        "referee": a.referee or PRIMARY_REFEREE.get(a.lang, a.lang),
        "graphify_baseline": bool(a.graphify),
        "truth_counts": truth["counts"],
        "entities": rows,
        # Diagnostic only: a (file, name) present in two kinds is credited to both.
        "kora_recall_by_kind": kind_rows,
        "extractor_planes": kora.get("planes", {}),
        "graphify_nodes_landing_on_a_truth_field": gf_field_hits,
        "graphify_field_hits_that_collide_with_a_method_or_type": gf_field_collisions,
        "preprocessor_disabled_nodes_excluded": disabled_excluded,
        "comment_planes": {
            "kora_rationale": sum(1 for n in k_nodes if n["node_type"] == "RATIONALE"),
            "kora_doc_ref": sum(1 for n in k_nodes if n["node_type"] == "DOC_REF"),
            "graphify_rationale": gf.get("aux", {}).get("rationale", 0),
            "graphify_doc_ref": gf.get("aux", {}).get("doc_ref", 0),
        },
        "method_line_accuracy_within_2": {
            "kora": f"{pct(k_ok, k_tot)}% ({k_ok}/{k_tot})",
            "graphify": f"{pct(g_ok, g_tot)}% ({g_ok}/{g_tot})",
        },
        "span_end_line_coverage": {
            "kora": f"{pct(spans_k, len(k_nodes))}% ({spans_k}/{len(k_nodes)})",
            "graphify": "0% — source_location is a start line only, no end",
        },
        "call_sites": {"kora_total": calls_k, "kora_with_receiver": calls_recv,
                       "kora_qualified_pct": pct(calls_recv, calls_k)},
        "node_totals": {"kora_java": len(k_nodes), "graphify_java_all_files": gf["node_count"]},
        # `.get`, not `[...]`: the released harness scores Graphify only and feeds this an empty
        # node set, which carries no timing key.
        "extract_ms": {"kora": kora.get("elapsed_ms")},
    }, indent=2))


if __name__ == "__main__":
    main()
