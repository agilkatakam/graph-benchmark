#!/usr/bin/env python
"""PRIMARY ground truth for SQL — a production parser per dialect, not one grammar for all.

Why this exists, and why it is the primary rather than the second referee.

`ast-referee.py --lang sql` runs tree-sitter-sql, which is the grammar Graphify itself parses
SQL with. On real SQL it errors over nearly all of the bytes of every repository in this corpus,
and on T-SQL stored-procedure repositories it finds no procedures at all. A referee that cannot
read the corpus cannot adjudicate it, so it is kept only as the second referee.

No single parser reads every SQL dialect, so this referee routes each file to a production
implementation for its dialect and reports the routing:

  postgres  -> pglast / libpg_query — PostgreSQL's own parser, the same C code the server uses
  tsql      -> Microsoft ScriptDom via scripts/ast-referee-scriptdom (see that file's header)
  mysql, sqlite, oracle, db2, ansi
            -> sqlglot, per-dialect, statement-sliced so one bad statement costs only itself

Inclusion rules are the ones fixed in ast-referee.py's `── SQL ──` header, restated here in
each parser's terms. Two rules matter more for SQL than they did for any earlier language:

  1. **A routine's body is not descended into.** A `CREATE TABLE #scratch` inside a stored
     procedure is a local, not schema — the same call every other language here makes for a
     variable declared inside a method. libpg_query gets this for free (a PL/pgSQL body is an
     opaque string to it); ScriptDom needs it enforced, and does — in a procedure-heavy
     repository nearly every apparent table is otherwise a procedure local.
  2. **CREATE, CREATE OR ALTER and ALTER of a routine all declare it, deduped per file+name.**
     T-SQL's universal idiom creates an empty stub inside an `EXEC('CREATE PROCEDURE ...')`
     string and then ALTERs it with the real body. Counting only CREATE scores that corpus at
     zero; counting both without dedupe doubles it.

Names are normalised the same way on every side (see `norm`): quoting stripped, schema
qualification dropped, lowercased — SQL identifiers are case-insensitive unless quoted, and
the tools qualify them differently.

  python3 ast-referee-sqlref.py <repoPath> [--out t.json]
"""
import collections
import json
import os
import re
import subprocess
import sys
import tempfile

SKIP = {".git", "node_modules", "target", "build", "vendor", ".gradle", "out",
        "dist", "coverage", "__pycache__"}

SCRIPTDOM = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "ast-referee-scriptdom", "bin", "Release", "net10.0", "sqlreferee.dll")


def _referee_version():
    from importlib.metadata import PackageNotFoundError, version
    parts = []
    for pkg in ("pglast", "sqlglot", "sqlfluff"):
        try:
            parts.append(f"{pkg} {version(pkg)}")
        except PackageNotFoundError:
            parts.append(f"{pkg} absent")
    sd = "scriptdom absent"
    if os.path.exists(SCRIPTDOM):
        try:
            sd = "scriptdom " + subprocess.run(
                ["dotnet", SCRIPTDOM, "--version"], capture_output=True, text=True,
                timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sd = "scriptdom present (version unavailable)"
    parts.append(sd)
    return " / ".join(parts)


# ── name normalisation ───────────────────────────────────────────────────────

_QUOTES = '"`[]'


def norm(name):
    """`[dbo].[sp_Blitz]` -> `sp_blitz`; `"public"."Fn"` -> `fn`; `Album` -> `album`."""
    if not name:
        return None
    s = str(name).strip()
    # Split on dots that are not inside a quoted part, then keep the last component.
    parts, buf, q = [], "", None
    for ch in s:
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
    last = parts[-1].strip()
    return last.lower() or None


# ── dialect routing ──────────────────────────────────────────────────────────
#
# Written down before any measurement. Path evidence wins over content evidence because a
# repository that ships the same schema for six engines names the files after them, and
# content markers overlap between dialects far more than filenames do.

PATH_DIALECT = [
    ("tsql", re.compile(r"sqlserver|mssql|t-?sql|azure", re.I)),
    ("mysql", re.compile(r"mysql|mariadb", re.I)),
    ("postgres", re.compile(r"postgres|postgre|pgsql|plpgsql", re.I)),
    ("sqlite", re.compile(r"sqlite", re.I)),
    ("oracle", re.compile(r"oracle|plsql", re.I)),
    ("databricks", re.compile(r"\bdb2\b", re.I)),
]

# Markers are ordered most-exclusive first and the first hit wins. MySQL leads because the
# backtick identifier and `ENGINE=` occur in no other dialect. `@@` is NOT a T-SQL marker:
# MySQL spells its system variables `@@sql_mode`, so it misroutes large MySQL dumps to
# ScriptDom, where they are unparseable.
CONTENT_DIALECT = [
    # `UNIQUE KEY (col)` as a table constraint and psql-style `source file.dump` are MySQL
    # client syntax and appear in no other dialect. Without them a MySQL dump carrying no other
    # marker defaults to libpg_query, which rejects MySQL-only constraint clauses and loses the
    # objects declared beside them.
    ("mysql", re.compile(r"`\w+`|ENGINE\s*=|AUTO_INCREMENT|^\s*DELIMITER\b|"
                         r"DEFAULT\s+CHARSET|\bUNIQUE\s+KEY\b|^\s*SOURCE\s+\S+\s*;", re.I | re.M)),
    ("tsql", re.compile(r"^\s*GO\s*$|\[dbo\]|NVARCHAR|IDENTITY\s*\(\s*\d|DECLARE\s+@|"
                        r"SET\s+ANSI_NULLS|BEGIN\s+TRY", re.I | re.M)),
    ("postgres", re.compile(r"\$\w*\$|plpgsql|\bSERIAL\b|RETURNS\s+SETOF|CREATE\s+EXTENSION|"
                            r"::\w+", re.I)),
    ("oracle", re.compile(r"\bVARCHAR2\b|\bNUMBER\s*\(\s*\d|^\s*/\s*$", re.I | re.M)),
    ("sqlite", re.compile(r"\bAUTOINCREMENT\b|^\s*PRAGMA\b", re.I | re.M)),
]

# Everything that is not routed to libpg_query or ScriptDom goes to sqlglot under this name.
SQLGLOT_DIALECT = {"mysql": "mysql", "sqlite": "sqlite", "oracle": "oracle",
                   "databricks": "databricks", "ansi": None}


def strip_noise(text):
    """Blank the *contents* of comments and string literals, keeping every byte position.

    Dialect detection has to run on code, not prose. Backticks appear in 34 PostgreSQL files
    of this corpus — inside comments (``-- `bucket_width` argument``) and inside error-message
    literals — and routing those to a MySQL parser cost timescaledb 30 of its 654 files and
    citus 5 of its 1,371 before this existed.
    """
    out = list(text)
    i, n = 0, len(text)

    def blank(lo, hi):
        # Newlines survive: blanking them merges lines and moves every marker that is
        # anchored to the start of one (`GO`, `DELIMITER`) off its line.
        for k in range(lo, min(hi, n)):
            if text[k] != "\n":
                out[k] = " "

    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if ch == "$":
            m = re.match(r"\$\w*\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                if j >= 0:
                    blank(i + len(tag), j)
                    i = j + len(tag)
                    continue
        # ONLY the single quote. A backtick and a double quote delimit *identifiers*, not
        # prose, and blanking them destroys the very evidence dialect detection runs on —
        # doing so silently rerouted sakila's MySQL schema to a PostgreSQL parser and cost
        # test-db 15 declarations.
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == ch:
                    break
                j += 1
            blank(i + 1, j)
            i = min(j + 1, n)
            continue
        i += 1
    return "".join(out)


def detect_dialect(path, text):
    for name, rx in PATH_DIALECT:
        if rx.search(path):
            return name
    code = strip_noise(text)
    for name, rx in CONTENT_DIALECT:
        if rx.search(code):
            return name
    # libpg_query is the most standards-complete parser of the three and the most permissive
    # on plain ANSI DDL, so an unmarked file goes there rather than to a guess.
    return "postgres"


# ── statement slicing ────────────────────────────────────────────────────────

# `[ \t]*`, not `\s*`: under re.M, `\s` matches the newline too, so the match ran back over the
# preceding blank lines and the length-preserving space substitution destroyed them. Every line
# the referee reported after the first psql meta-command in a file was one or more lines early
# — silent, because the declarations themselves were all still found. It surfaced only when
# routine-body line ranges started depending on the number being right.
PSQL_META = re.compile(r"^[ \t]*\\.*$", re.M)

# psql's `\gset` / `\gexec` / `\g` *terminate* the statement they follow, and they sit at the
# end of a line rather than the start, so the meta-command blanker above does not see them.
# Left in place, `SELECT ... \gset` swallows the next statement — which in timescaledb's
# regress suite is repeatedly a `CREATE FUNCTION`, and the referee then reported neither the
# function nor an error, because the merged chunk no longer *starts* with CREATE.
PSQL_GTERM = re.compile(r"\\g(?:set|exec|desc|describe|x)?\b", re.I)

# psql's `\copy ... FROM STDIN` (and server-side `COPY ... FROM STDIN`) is followed by raw
# data rows terminated by a lone `\.`. Those rows are not SQL, and leaving them in merges the
# next real statement into an unparseable chunk — which is how citus's materialized_view.sql
# lost its only materialized view.
#
# Done line by line rather than with one regex: a `.*?` spanning to the next `\.` runs to the
# END OF THE FILE when a block has no terminator, and citus's multi_modifying_xacts.sql has
# exactly that — one unterminated block blanked 5,000 lines and 10 real tables with them.
COPY_STDIN_START = re.compile(r"^[ \t]*(?:\\)?copy\b[^\n]*\bfrom\s+stdin\b", re.I)


def blank_copy_data(text):
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        out.append(lines[i])
        if COPY_STDIN_START.match(lines[i]):
            # Look for the terminator first; blank only if this block actually has one.
            # The scan stops at a blank line or at another backslash meta-command, because
            # `COPY x FROM STDIN;` also appears with NO inline data — the client sends it —
            # and searching on to the file's next `\.` blanked 105 lines of real SQL in
            # citus's multi_modifying_xacts.sql, including 10 CREATE TABLEs.
            end = None
            for j in range(i + 1, len(lines)):
                stripped = lines[j].strip()
                if stripped == "\\.":
                    end = j
                    break
                if not stripped or stripped.startswith("\\"):
                    break
            if end is not None:
                for j in range(i + 1, end):
                    out.append(" " * len(lines[j]))
                out.append(lines[end])
                i = end + 1
                continue
        i += 1
    return "\n".join(out)


def strip_noise(text):
    """Blank the *contents* of comments and string literals, keeping every byte position.

    Dialect detection has to run on code, not prose. Backticks appear in 34 PostgreSQL files
    of this corpus — inside comments (``-- `bucket_width` argument``) and inside error-message
    literals — and routing those to a MySQL parser cost timescaledb 30 of its 654 files and
    citus 5 of its 1,371 before this existed.
    """
    out = list(text)
    i, n = 0, len(text)

    def blank(lo, hi):
        # Newlines survive: blanking them merges lines and moves every marker that is
        # anchored to the start of one (`GO`, `DELIMITER`) off its line.
        for k in range(lo, min(hi, n)):
            if text[k] != "\n":
                out[k] = " "

    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if ch == "$":
            m = re.match(r"\$\w*\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                if j >= 0:
                    blank(i + len(tag), j)
                    i = j + len(tag)
                    continue
        # ONLY the single quote. A backtick and a double quote delimit *identifiers*, not
        # prose, and blanking them destroys the very evidence dialect detection runs on —
        # doing so silently rerouted sakila's MySQL schema to a PostgreSQL parser and cost
        # test-db 15 declarations.
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == ch:
                    break
                j += 1
            blank(i + 1, j)
            i = min(j + 1, n)
            continue
        i += 1
    return "".join(out)


def detect_dialect(path, text):
    for name, rx in PATH_DIALECT:
        if rx.search(path):
            return name
    code = strip_noise(text)
    for name, rx in CONTENT_DIALECT:
        if rx.search(code):
            return name
    # libpg_query is the most standards-complete parser of the three and the most permissive
    # on plain ANSI DDL, so an unmarked file goes there rather than to a guess.
    return "postgres"


# ── statement slicing ────────────────────────────────────────────────────────

# `[ \t]*`, not `\s*`: under re.M, `\s` matches the newline too, so the match ran back over the
# preceding blank lines and the length-preserving space substitution destroyed them. Every line
# the referee reported after the first psql meta-command in a file was one or more lines early
# — silent, because the declarations themselves were all still found. It surfaced only when
# routine-body line ranges started depending on the number being right.
PSQL_META = re.compile(r"^[ \t]*\\.*$", re.M)

# psql's `\gset` / `\gexec` / `\g` *terminate* the statement they follow, and they sit at the
# end of a line rather than the start, so the meta-command blanker above does not see them.
# Left in place, `SELECT ... \gset` swallows the next statement — which in timescaledb's
# regress suite is repeatedly a `CREATE FUNCTION`, and the referee then reported neither the
# function nor an error, because the merged chunk no longer *starts* with CREATE.
PSQL_GTERM = re.compile(r"\\g(?:set|exec|desc|describe|x)?\b", re.I)

# psql's `\copy ... FROM STDIN` (and server-side `COPY ... FROM STDIN`) is followed by raw
# data rows terminated by a lone `\.`. Those rows are not SQL, and leaving them in merges the
# next real statement into an unparseable chunk — which is how citus's materialized_view.sql
# lost its only materialized view. Blanked as a block, newlines kept.
COPY_STDIN_BLOCK = re.compile(
    r"^([ \t]*(?:\\)?copy\b[^\n]*\bfrom\s+stdin\b[^\n]*\n)(.*?)(^[ \t]*\\\.[ \t]*$)",
    re.I | re.M | re.S)


def blank_psql_meta(text):
    """psql backslash commands are not SQL and abort libpg_query's scanner. Replaced with
    whitespace of the same length so every byte offset — and therefore every line — is
    preserved."""
    text = blank_copy_data(text)
    text = PSQL_META.sub(lambda m: " " * len(m.group(0)), text)
    return PSQL_GTERM.sub(lambda m: ";" + " " * (len(m.group(0)) - 1), text)


TEMPLATE_TOKEN = re.compile(r"@\w+@")


def expand_templates(text):
    """`@extschema@.detach_chunk(...)` is not SQL — it is a build-time placeholder that
    PostgreSQL extensions substitute before install, and timescaledb writes its entire
    product surface that way: 470 declaring statements, including every CREATE FUNCTION in
    `sql/`, fail to parse with the token left in. Replaced with an identifier of exactly the
    same length so every byte offset, and therefore every reported line, is unchanged. The
    substituted name is a schema qualifier, and name matching drops the qualifier, so this
    cannot change which declaration a statement is credited with."""
    return TEMPLATE_TOKEN.sub(lambda m: "e" + "_" * (len(m.group(0)) - 1), text)


# `(?<![:\w])` keeps this off PostgreSQL's `::` cast and off array slices (`arr[1:2]`).
PSQL_VAR = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def expand_psql_vars(text):
    """A psql *client* variable, substituted before the server ever sees the statement. Both
    forms are length-preserving so offsets, and therefore lines, are untouched.

    Which expansion is correct depends on the position, and the referee cannot know it:
    `CREATE FUNCTION f() RETURNS VOID AS :MODULE_PATHNAME LANGUAGE C` — 244 of timescaledb's
    test helpers — needs a string literal, while `CREATE VIEW v AS SELECT * FROM :chunk_table`
    needs an identifier. So both are tried, in that order, and the first that parses wins."""
    return PSQL_VAR.sub(lambda m: "'" + m.group(0)[1:-1] + "'", text)


def expand_psql_vars_ident(text):
    return PSQL_VAR.sub(lambda m: "v" + "_" * (len(m.group(0)) - 1), text)


# Whitespace and comments before the first real token of a statement. The offset a splitter
# hands back is the byte after the previous terminator, which is typically a blank line and a
# `-- comment` header above the declaration — six lines above it in test-db's objects.sql. The
# line a referee reports has to be the declaration's own, or every line comparison measures
# comment style rather than placement.
LEADING_NOISE = re.compile(r"\A(?:\s+|--[^\n]*|/\*.*?\*/|#[^\n]*)+", re.S)


def code_start(chunk):
    m = LEADING_NOISE.match(chunk)
    return m.end() if m else 0


def line_at(text, offset):
    return text.count("\n", 0, offset) + 1


def split_generic(text):
    """(offset, statement) pairs for the dialects sqlglot handles. Understands string
    literals, backtick/bracket identifiers, both comment forms, and MySQL's DELIMITER — which
    exists precisely so a stored routine's body can contain semicolons, and without which
    every MySQL procedure is shredded into fragments."""
    out = []
    delim = ";"
    i, n = 0, len(text)
    start = 0

    def emit(end):
        raw = text[start:end]
        off = code_start(raw)
        stmt = raw[off:].strip()
        if stmt:
            out.append((start + off, stmt))
    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "$":
            dq = re.match(r"\$\w*\$", text[i:])
            if dq:
                tag = dq.group(0)
                j = text.find(tag, i + len(tag))
                i = n if j < 0 else j + len(tag)
                continue
        if ch in "'\"`":
            q, i = ch, i + 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if ch == "[":
            j = text.find("]", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "\n" or i == 0:
            probe = i + 1 if ch == "\n" else i
            m = re.match(r"[ \t]*DELIMITER[ \t]+(\S+)[ \t]*\r?\n", text[probe:], re.I)
            if m:
                emit(i)
                delim = m.group(1)
                i = probe + m.end()
                start = i
                continue
        if text.startswith(delim, i):
            emit(i)
            i += len(delim)
            start = i
            continue
        i += 1
    emit(n)
    return out


# ── engines ──────────────────────────────────────────────────────────────────

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$")
_BEGIN_KW = re.compile(r"\bBEGIN\b", re.I)


class Sink:
    """Collects declarations and applies the dedupe rule: types and methods are one node per
    (file, name) — matching Graphify's own node-id dedupe, so a re-declared object is one
    node on every side — and a field is one per (file, owner, name)."""

    # AST_SQL_NO_DEDUPE=1 turns the dedupe OFF, so ast-sensitivity.py can measure what the rule
    # is worth to each side.
    _DEDUPE = os.environ.get("AST_SQL_NO_DEDUPE") != "1"

    def __init__(self):
        self.types, self.methods, self.fields = [], [], []
        self.unadjudicated_ranges = collections.defaultdict(list)
        self._seen = set()

    def unadjudicated(self, file, lo, hi):
        """A line range no available parser for this dialect could read. Excluded from BOTH
        sides by ast-layer-compare.py — see this file's header."""
        self.unadjudicated_ranges[file].append([lo, hi])

    def routine_body(self, file, chunk, start_line):
        """A routine's body is out of the taxonomy for EVERY tool, so its lines are excluded
        for every tool — the rule collect_tsql already applied via ScriptDom's
        `routine_body_ranges`. It was applied to T-SQL only, so on the PostgreSQL and generic
        dialects the same construct was still charged: 211 of Graphify's unmatched SQL nodes
        are `CREATE TABLE`/`CREATE INDEX` statements inside a `$$ ... $$` plpgsql body or a
        MySQL `BEGIN ... END` trigger. Same definitional difference, opposite treatment,
        decided only by which dialect the file happened to be written in.

        Anchored on the body's opening delimiter, not on the statement's first line. Excluding
        everything after the header line instead swallowed the routine's OWN declaration
        whenever its name sat on a continuation line (`CREATE OR REPLACE FUNCTION\\n  foo(...)`)
        — the tools report such a routine on the name's line and the referee on the statement's,
        so the exclusion removed the extraction while leaving the truth entry, and PostgreSQL
        method recall read 74% for both tools at once. A drop that lands on both sides equally
        is the signature of a referee bug, not of two systems failing together."""
        mo = _DOLLAR_TAG.search(chunk)
        if mo:
            tag = mo.group(0)
            close = chunk.find(tag, mo.end())
            lo = start_line + chunk.count("\n", 0, mo.end())
            hi = start_line + chunk.count("\n", 0, close if close >= 0 else len(chunk))
        else:
            mo = _BEGIN_KW.search(chunk)
            if not mo:
                return
            lo = start_line + chunk.count("\n", 0, mo.end())
            hi = start_line + chunk.count("\n")
        lo = max(lo + 1, start_line + 1)
        if hi >= lo:
            self.unadjudicated_ranges[file].append([lo, hi])

    def type(self, file, name, kind, line):
        name = norm(name)
        if not name or (self._DEDUPE and ("t", file, name) in self._seen):
            return
        self._seen.add(("t", file, name))
        self.types.append({"file": file, "name": name, "kind": kind, "line": line})

    def method(self, file, name, kind, line, params=""):
        name = norm(name)
        if not name or (self._DEDUPE and ("m", file, name) in self._seen):
            return
        self._seen.add(("m", file, name))
        self.methods.append({"file": file, "name": name, "owner": None, "kind": kind,
                             "line": line, "params": params})

    def field(self, file, name, owner, kind, line):
        name, owner = norm(name), norm(owner)
        if not name or (self._DEDUPE and ("f", file, owner, name) in self._seen):
            return
        self._seen.add(("f", file, owner, name))
        self.fields.append({"file": file, "name": name, "owner": owner, "kind": kind,
                            "type": None, "line": line})


PG_STMT_HANDLERS = {}


# pglast's enum members stringify to their *number* (`str(kind)` is `"49"`), not their name —
# `repr` is what shows `<ObjectType.OBJECT_TYPE: 49>`. Three membership tests here were written
# against `str()` and were therefore always false, which silently cost the referee every base
# type, every materialized view (they were filed as tables) and nearly every ALTER TABLE ADD
# COLUMN. Read `.name`.
def _enum_name(value):
    return getattr(value, "name", "") or ""


def collect_postgres(text, rel, sink, stats):
    from pglast import parse_sql, split
    from pglast import ast as pgast

    clean = expand_psql_vars(expand_templates(blank_psql_meta(text)))
    clean_ident = expand_psql_vars_ident(expand_templates(blank_psql_meta(text)))
    try:
        chunks = [(sl.start, clean[sl.start:sl.stop])
                  for sl in split(clean, with_parser=False, only_slices=True)]
    except Exception:
        # PostgreSQL's own scanner is a *lexer*, so one malformed token anywhere aborts the
        # whole file, not one statement: `9copy` in timescaledb's compression_insert.sql and a
        # zero-length quoted identifier in citus's pg17.sql each took every declaration in
        # their file to zero. Fall back to the dialect-neutral splitter, which only has to
        # find statement boundaries, not tokenise.
        stats["slice_errors"] += 1
        chunks = split_generic(clean)
    for base, chunk in chunks:
        if not chunk.strip():
            continue
        off = code_start(chunk)
        base, chunk = base + off, chunk[off:]
        try:
            parsed = parse_sql(chunk)
        except Exception:
            try:
                parsed = parse_sql(clean_ident[base:base + len(chunk)])
            except Exception:
                parsed = None
        if parsed is None:
            # Only a *declaring* statement that will not parse is a hole in the truth. A
            # regress-suite `SELECT` written with psql client variables (`:PREFIX SELECT ...`)
            # is 1,044 of timescaledb's parse failures and declares nothing, so counting it
            # would make the referee look blind where it is merely reading a test script.
            if DECLARES.search(chunk):
                stats["stmt_errors"] += 1
                stats["error_bytes"] += len(chunk.encode("utf8", "replace"))
                lo = line_at(clean, base)
                sink.unadjudicated(rel, lo, lo + chunk.count("\n"))
            else:
                stats["non_declaring_errors"] += 1
            continue
        for raw in parsed:
            s = raw.stmt
            line = line_at(clean, base)

            def fline(node):
                loc = getattr(node, "location", None)
                return line_at(clean, base + loc) if isinstance(loc, int) and loc >= 0 else line

            if isinstance(s, pgast.CreateForeignTableStmt):
                base_stmt = s.base
                nm = base_stmt.relation.relname
                sink.type(rel, nm, "table", line)
                for el in (base_stmt.tableElts or ()):
                    if isinstance(el, pgast.ColumnDef) and el.colname:
                        sink.field(rel, el.colname, nm, "column", fline(el))
            elif isinstance(s, pgast.CreateStmt):
                nm = s.relation.relname
                sink.type(rel, nm, "table", line)
                for el in (s.tableElts or ()):
                    if isinstance(el, pgast.ColumnDef) and el.colname:
                        sink.field(rel, el.colname, nm, "column", fline(el))
            elif isinstance(s, pgast.ViewStmt):
                sink.type(rel, s.view.relname, "view", line)
            elif isinstance(s, pgast.CreateTableAsStmt):
                into = s.into
                kind = "materialized_view" if "MATVIEW" in _enum_name(s.objtype) else "table"
                if into is not None and into.rel is not None:
                    sink.type(rel, into.rel.relname, kind, line)
            elif isinstance(s, pgast.CreateFunctionStmt):
                nm = s.funcname[-1].sval if s.funcname else None
                params = ", ".join(p.name or "" for p in (s.parameters or ()))
                sink.method(rel, nm, "procedure" if s.is_procedure else "function", line, params)
                sink.routine_body(rel, chunk, line)
            elif isinstance(s, pgast.CreateTrigStmt):
                sink.type(rel, s.trigname, "trigger", line)
                sink.routine_body(rel, chunk, line)
            elif isinstance(s, pgast.CreateEnumStmt):
                nm = s.typeName[-1].sval if s.typeName else None
                sink.type(rel, nm, "type", line)
                for v in (s.vals or ()):
                    sink.field(rel, v.sval, nm, "enum_label", line)
            elif isinstance(s, pgast.CompositeTypeStmt):
                nm = s.typevar.relname
                sink.type(rel, nm, "type", line)
                for el in (s.coldeflist or ()):
                    if isinstance(el, pgast.ColumnDef) and el.colname:
                        sink.field(rel, el.colname, nm, "type_attribute", fline(el))
            elif isinstance(s, pgast.CreateDomainStmt):
                nm = s.domainname[-1].sval if s.domainname else None
                sink.type(rel, nm, "type", line)
            elif isinstance(s, pgast.CreateRangeStmt):
                nm = s.typeName[-1].sval if s.typeName else None
                sink.type(rel, nm, "type", line)
            elif isinstance(s, pgast.DefineStmt) and _enum_name(s.kind) == "OBJECT_TYPE":
                # `CREATE TYPE x (INPUT = f_in, OUTPUT = f_out, ...)` — a base type. It
                # declares a named type with no attributes; the parenthesised body is a
                # property list, not a column list. libpg_query models it as a DefineStmt
                # rather than a Create*Stmt, and missing it made 25 correct extractions on
                # timescaledb and citus score as fabrications.
                nm = s.defnames[-1].sval if s.defnames else None
                sink.type(rel, nm, "type", line)
            elif isinstance(s, pgast.CreateSeqStmt):
                sink.type(rel, s.sequence.relname, "sequence", line)
            elif isinstance(s, pgast.DoStmt):
                # An anonymous `DO $$ ... $$` block is a routine body without a name. Same
                # taxonomy status as a named one, so the same exclusion — and the header line
                # declares nothing, so the whole statement goes.
                sink.unadjudicated(rel, line, line + chunk.count("\n"))
            elif isinstance(s, pgast.AlterTableStmt):
                owner = s.relation.relname if s.relation is not None else None
                for cmd in (s.cmds or ()):
                    if _enum_name(cmd.subtype) != "AT_AddColumn":
                        continue
                    d = cmd.def_
                    if isinstance(d, pgast.ColumnDef) and d.colname:
                        sink.field(rel, d.colname, owner, "added_column", fline(d))


SQLGLOT_KINDS = {"TABLE": "table", "VIEW": "view", "MATERIALIZED VIEW": "materialized_view",
                 "SEQUENCE": "sequence", "TYPE": "type", "TRIGGER": "trigger",
                 "FUNCTION": "function", "PROCEDURE": "procedure"}


def _sqlglot_name(node):
    from sqlglot import exp
    if node is None:
        return None
    if isinstance(node, exp.Schema):
        return _sqlglot_name(node.this)
    if isinstance(node, (exp.Table, exp.UserDefinedFunction, exp.Identifier, exp.Index)):
        return node.name or _sqlglot_name(node.this)
    return getattr(node, "name", None) or None


# ── the generic engine: sqlglot, then sqlfluff, then unadjudicated ───────────
#
# Neither of the two available multi-dialect parsers reads this slice of the corpus on its
# own, and they fail in opposite directions — measured, not assumed:
#
#   Chinook_Sqlite.sql   sqlglot parses 57/57 statements; sqlfluff parses 0/57
#   sakila (MySQL)       sqlfluff finds 16 tables, 7 views and 3 triggers;
#                        sqlglot finds 15 tables, 6 views and 0 triggers
#
# So a chunk is offered to sqlglot first and to sqlfluff if sqlglot raises. This is coverage,
# not answer-shopping: both are independent of the systems under test, and neither is asked
# which answer it prefers — only whether it can read the statement at all.
#
# A chunk NEITHER can parse becomes an **unadjudicated line range**, and
# ast-layer-compare.py excludes those ranges from every system alike — the same mechanism used
# for `#if`-disabled regions in C#. No available parser reads MySQL stored routines whose body
# sits behind a custom DELIMITER, and scoring either tool's recovery of them as a false positive
# would measure the referee's blind spot.

SQLFLUFF_KIND = {
    "create_table_statement": "table",
    "create_view_statement": "view",
    "create_materialized_view_statement": "materialized_view",
    "create_sequence_statement": "sequence",
    "create_type_statement": "type",
    "create_domain_statement": "type",
    "create_trigger": "trigger",
    "create_trigger_statement": "trigger",
    "create_function_statement": "function",
    "create_procedure_statement": "procedure",
}

SQLFLUFF_NAME_SEG = ("table_reference", "object_reference", "function_name", "trigger_reference",
                     "view_reference", "sequence_reference", "extension_reference")
SQLFLUFF_IDENT = ("naked_identifier", "quoted_identifier", "identifier")

_SQLFLUFF_LINTERS = {}


def _sqlfluff_linter(dialect):
    if dialect not in _SQLFLUFF_LINTERS:
        from sqlfluff.core import FluffConfig, Linter
        _SQLFLUFF_LINTERS[dialect] = Linter(
            config=FluffConfig(overrides={"dialect": dialect or "ansi", "rules": "None"}))
    return _SQLFLUFF_LINTERS[dialect]


def _sf_descendants(seg, types):
    for s in seg.recursive_crawl(*types):
        yield s


def _sf_name(seg):
    for s in seg.recursive_crawl(*SQLFLUFF_NAME_SEG):
        return s.raw
    for s in seg.recursive_crawl(*SQLFLUFF_IDENT):
        return s.raw
    return None


def collect_sqlfluff_chunk(chunk, rel, sink, base_line, dialect):
    """True when sqlfluff read the chunk with no unparsable section."""
    parsed = _sqlfluff_linter(dialect).parse_string(chunk)
    if parsed.violations or parsed.tree is None:
        return False
    for stmt in parsed.tree.recursive_crawl("statement"):
        for node in stmt.segments:
            kind = SQLFLUFF_KIND.get(node.get_type())
            if kind is None:
                continue
            nm = _sf_name(node)
            line = base_line + (node.pos_marker.working_line_no - 1 if node.pos_marker else 0)
            if kind in ("function", "procedure"):
                sink.method(rel, nm, kind, line)
                continue
            sink.type(rel, nm, kind, line)
            if kind in ("table", "type"):
                for cd in _sf_descendants(node, ("column_definition",)):
                    cn = next((c.raw for c in cd.segments if c.get_type() in SQLFLUFF_IDENT), None)
                    cl = base_line + (cd.pos_marker.working_line_no - 1 if cd.pos_marker else 0)
                    sink.field(rel, cn, nm, "column" if kind == "table" else "type_attribute", cl)
        for node in stmt.segments:
            if node.get_type() != "alter_table_statement":
                continue
            owner = _sf_name(node)
            for cd in _sf_descendants(node, ("column_definition",)):
                cn = next((c.raw for c in cd.segments if c.get_type() in SQLFLUFF_IDENT), None)
                cl = base_line + (cd.pos_marker.working_line_no - 1 if cd.pos_marker else 0)
                sink.field(rel, cn, owner, "added_column", cl)
    return True


# A chunk that starts with CREATE or ALTER is expected to yield a declaration. sqlglot does
# not raise on syntax it cannot model — it degrades the statement to a `Command` node — so
# "parsed without error" is not evidence it was read. Silence on a declaring statement is the
# signal to hand the chunk to the second parser.
# A chunk *containing* a declaration head, not merely starting with one. Statement splitting
# is imperfect on client-side syntax, and when two statements merge the declaration is no
# longer first — so "does not start with CREATE" is not evidence that nothing was declared,
# and treating it as evidence let real losses go unrecorded instead of unadjudicated.
DECLARES = re.compile(
    r"\b(?:CREATE|ALTER)\s+(?:OR\s+\w+\s+)?(?:\w+\s+)?"
    r"(?:TABLE|VIEW|MATERIALIZED|FUNCTION|PROCEDURE|TRIGGER|TYPE|DOMAIN|SEQUENCE)\b", re.I)


# MySQL's object-definition preamble. sqlglot reads `CREATE DEFINER=CURRENT_USER SQL SECURITY
# INVOKER VIEW actor_info` and reports the view's name as `current_user` — it takes the
# DEFINER's value for the object name. Blanked (length-preserving) before parsing, which is a
# dialect normalisation, not a correction toward either tool: the name in that statement is
# `actor_info` under any reading, and both production referees get it right.
MYSQL_PREAMBLE = re.compile(
    r"DEFINER\s*=\s*(?:CURRENT_USER(?:\s*\(\s*\))?|`[^`]*`(?:@`[^`]*`)?|'[^']*'(?:@'[^']*')?|[\w$]+(?:@[\w$.%'\"`]+)?)"
    r"|SQL\s+SECURITY\s+(?:DEFINER|INVOKER)"
    r"|ALGORITHM\s*=\s*\w+", re.I)


def collect_generic(text, rel, sink, stats, dialect):
    import sqlglot
    from sqlglot import exp

    for offset, raw_chunk in split_generic(text):
        chunk = MYSQL_PREAMBLE.sub(lambda m: " " * len(m.group(0)), raw_chunk)
        line = line_at(text, offset)
        emitted = False
        try:
            s = sqlglot.parse_one(chunk, read=dialect)
        except Exception:
            s = None
        if s is not None:
            for node in ([s] if isinstance(s, exp.Create) else list(s.find_all(exp.Create))):
                kind = SQLGLOT_KINDS.get((node.kind or "").upper())
                if kind is None:
                    continue
                nm = _sqlglot_name(node.this)
                if not nm:
                    continue
                emitted = True
                if kind in ("function", "procedure"):
                    sink.method(rel, nm, kind, line)
                    sink.routine_body(rel, chunk, line)
                    continue
                if kind == "trigger":
                    sink.routine_body(rel, chunk, line)
                sink.type(rel, nm, kind, line)
                # ColumnDefs are only columns under a table/type Schema. A function's
                # parameters are ColumnDef nodes too, which is why this does not use
                # find_all on the Create.
                if kind in ("table", "type") and isinstance(node.this, exp.Schema):
                    for cd in node.this.expressions:
                        if isinstance(cd, exp.ColumnDef) and cd.name:
                            sink.field(rel, cd.name, nm,
                                       "column" if kind == "table" else "type_attribute", line)
            if isinstance(s, exp.Alter):
                owner = _sqlglot_name(s.this)
                for act in (s.args.get("actions") or []):
                    if isinstance(act, exp.ColumnDef) and act.name:
                        emitted = True
                        sink.field(rel, act.name, owner, "added_column", line)
                # An ALTER that adds a constraint or an index declares nothing under this
                # taxonomy, and must not be sent to the second parser as if it had failed.
                if not emitted:
                    emitted = True

        if emitted or not DECLARES.search(chunk):
            continue
        try:
            if collect_sqlfluff_chunk(chunk, rel, sink, line, dialect):
                continue
        except Exception:
            pass
        stats["stmt_errors"] += 1
        stats["error_bytes"] += len(chunk.encode("utf8", "replace"))
        sink.unadjudicated(rel, line, line + chunk.count("\n"))


def collect_tsql(paths, root, sink, stats):
    """One ScriptDom process for every T-SQL file in the repo — dotnet start-up dominates a
    per-file invocation."""
    if not os.path.exists(SCRIPTDOM):
        stats["missing_scriptdom"] = True
        return
    # Fixed $TMPDIR names before: two repositories adjudicated concurrently wrote the same
    # listing and the same output, and the second read whichever finished first. It fails as a
    # wrong truth file, not as an error.
    fd, listing = tempfile.mkstemp(prefix="sqlref_tsql_files_", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(paths))
    fd, outp = tempfile.mkstemp(prefix="sqlref_tsql_out_", suffix=".json")
    os.close(fd)
    subprocess.run(["dotnet", SCRIPTDOM, "--files", listing, "--root", root, "--out", outp],
                   check=True, stderr=subprocess.DEVNULL)
    d = json.load(open(outp))
    for t in d["types"]:
        sink.type(t["file"], t["name"], t["kind"], t["line"])
    for m in d["methods"]:
        sink.method(m["file"], m["name"], m["kind"], m["line"])
    for f in d["fields"]:
        sink.field(f["file"], f["name"], f["owner"], f["kind"], f["line"])
    stats["stmt_errors"] += d["parse_errors"]
    stats["error_bytes"] += d["error_bytes"]
    stats["scriptdom_error_files"] = d["error_files"]
    # ScriptDom recovers and keeps parsing past an error, so a file with any error has an
    # unknown-size hole in it. Marking the whole file unadjudicated is the conservative call: it
    # can only withhold credit from a system, never award it.
    for f in d["error_files"]:
        sink.unadjudicated(f, 1, 10 ** 9)
    # A routine body is out of the taxonomy for EVERY tool, so its lines are excluded from every
    # tool. Without this, procedure-local `CREATE TABLE #scratch` nodes score as fabricated
    # schema — a definitional difference read as an extraction failure.
    for f, ranges in (d.get("routine_body_ranges") or {}).items():
        for lo, hi in ranges:
            sink.unadjudicated(f, lo, hi)


# ── driver ───────────────────────────────────────────────────────────────────

def walk_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for f in filenames:
            if f.lower().endswith(".sql"):
                yield os.path.join(dirpath, f)


def main():
    repo = sys.argv[1]
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None

    sink = Sink()
    stats = collections.defaultdict(int)
    dialects = collections.Counter()
    tsql_paths = []
    total_bytes = 0
    files = 0

    for path in sorted(walk_files(repo)):
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        text = raw.decode("utf8", "replace")
        rel = os.path.relpath(path, repo)
        files += 1
        total_bytes += len(raw)
        d = detect_dialect(rel, text)
        dialects[d] += 1
        if d == "tsql":
            tsql_paths.append(path)
        elif d == "postgres":
            collect_postgres(text, rel, sink, stats)
        else:
            collect_generic(text, rel, sink, stats, SQLGLOT_DIALECT.get(d))

    if tsql_paths:
        collect_tsql(tsql_paths, repo, sink, stats)

    kinds = {}
    for plane in ("types", "methods", "fields"):
        for item in getattr(sink, plane):
            k = item.get("kind")
            if k:
                kinds[f"{plane}:{k}"] = kinds.get(f"{plane}:{k}", 0) + 1

    out = {
        "repo": repo, "lang": "sql", "referee": "dialect-routed (libpg_query / ScriptDom / sqlglot)",
        # Three parsers decide SQL truth and each pins a different upstream — pglast pins a
        # PostgreSQL major through libpg_query, ScriptDom pins a T-SQL grammar, sqlglot its own.
        # A reader on different versions produces their own truth set rather than this one.
        "referee_version": _referee_version(),
        "files": files, "total_bytes": total_bytes,
        "dialects": dict(dialects),
        "types": sink.types, "methods": sink.methods, "fields": sink.fields, "constants": [],
        # `disabled_ranges` is ast-layer-compare.py's existing contract for "the referee
        # cannot adjudicate these lines; exclude them from both sides". C# fills it with
        # `#if`-disabled regions; SQL fills it with statements no parser for the dialect
        # could read.
        "disabled_ranges": {k: v for k, v in sink.unadjudicated_ranges.items()},
        "counts": {
            "files": files, "types": len(sink.types), "methods": len(sink.methods),
            "fields": len(sink.fields), "constants": 0,
            "parse_errors": stats["stmt_errors"],
            "error_byte_pct": (round(100.0 * stats["error_bytes"] / total_bytes, 2)
                               if total_bytes else 0.0),
        },
        "kind_breakdown": dict(sorted(kinds.items())),
        "unparsed_statements": stats["stmt_errors"],
    }
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(out, fh)
    print(json.dumps({**out["counts"], "dialects": out["dialects"],
                      "kinds": out["kind_breakdown"]}, indent=2))


if __name__ == "__main__":
    main()
