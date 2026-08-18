// T-SQL ground truth, from Microsoft's own T-SQL parser (ScriptDom) — the same component
// SQL Server Management Studio and sqlpackage parse with.
//
// Why this exists. The two general-purpose SQL parsers available to this benchmark both fail on
// real T-SQL: tree-sitter-sql (the grammar Graphify parses with) errors over essentially all of
// the bytes of a T-SQL corpus and finds none of its stored procedures, and sqlglot raises a hard
// ParseError on the same files. ScriptDom is the definition of what a T-SQL declaration is, and
// neither system under test uses it.
//
// Implements the SQL inclusion rules fixed in ast-referee.py's `── SQL ──` header, in
// ScriptDom's terms. One extra rule the other dialects do not need:
//
//   CREATE / CREATE OR ALTER / ALTER of a FUNCTION, PROCEDURE or TRIGGER all declare the
//   routine. T-SQL's universal idiom is to create an empty stub inside an EXEC('...') string
//   and then ALTER it with the real body: the CREATE sits inside a string literal and is not a
//   declaration at all, so counting only CREATE scores such a corpus at zero. Names are deduped
//   per (file, name), so the stub-then-alter pair is one routine, not two.
//
//   dotnet <sqlreferee.dll> --files list.txt --root <repo> --out t.json

using System.Text;
using System.Text.Json;
using Microsoft.SqlServer.TransactSql.ScriptDom;

// ScriptDom's T-SQL grammar changes between package versions, so the SQL truth set has to be
// able to name the parser that produced it. ast-referee-sqlref.py asks for this and records
// the answer in every truth file.
if (args.Length == 1 && args[0] == "--version")
{
    Console.WriteLine("Microsoft.SqlServer.TransactSql.ScriptDom "
        + (typeof(TSql160Parser).Assembly.GetName().Version?.ToString() ?? "unknown"));
    return;
}

string filesList = null, root = null, outPath = null;
for (var i = 0; i < args.Length - 1; i++)
{
    if (args[i] == "--files") filesList = args[i + 1];
    if (args[i] == "--root") root = args[i + 1];
    if (args[i] == "--out") outPath = args[i + 1];
}

var types = new List<Dictionary<string, object>>();
var methods = new List<Dictionary<string, object>>();
var fields = new List<Dictionary<string, object>>();
// Line ranges covered by a routine BODY. The taxonomy excludes declarations inside one, so the
// comparison excludes those lines from every tool rather than scoring a procedure-local
// `CREATE TABLE #scratch` as a fabricated schema object — that is a definitional difference,
// not an extraction error.
var bodyRanges = new Dictionary<string, List<int[]>>();
int fileCount = 0, parseErrors = 0;
long totalBytes = 0, errorBytes = 0;
var errorFiles = new List<string>();

foreach (var path in File.ReadLines(filesList))
{
    if (string.IsNullOrWhiteSpace(path)) continue;
    string text;
    try { text = File.ReadAllText(path); } catch { continue; }
    fileCount++;
    totalBytes += Encoding.UTF8.GetByteCount(text);
    var rel = Path.GetRelativePath(root, path);

    // 170 is the newest dialect level; it is a superset of the older ones for parsing
    // purposes. `true` = quoted identifiers on, which is the SQL Server default.
    var parser = new TSql170Parser(true);
    IList<ParseError> errors;
    TSqlFragment frag;
    using (var rdr = new StringReader(text)) frag = parser.Parse(rdr, out errors);
    if (errors != null && errors.Count > 0)
    {
        parseErrors++;
        errorBytes += Encoding.UTF8.GetByteCount(text);
        errorFiles.Add(rel);
    }
    if (frag == null) continue;

    var v = new Collector(rel, types, methods, fields, bodyRanges);
    frag.Accept(v);
}

var payload = new Dictionary<string, object>
{
    ["engine"] = "scriptdom",
    ["files"] = fileCount,
    ["parse_errors"] = parseErrors,
    ["total_bytes"] = totalBytes,
    ["error_bytes"] = errorBytes,
    ["error_files"] = errorFiles,
    ["types"] = types,
    ["methods"] = methods,
    ["fields"] = fields,
    ["routine_body_ranges"] = bodyRanges,
};
var json = JsonSerializer.Serialize(payload);
if (outPath != null) File.WriteAllText(outPath, json); else Console.WriteLine(json);
Console.Error.WriteLine($"scriptdom files={fileCount} parse_errors={parseErrors} " +
                        $"types={types.Count} methods={methods.Count} fields={fields.Count}");

sealed class Collector : TSqlFragmentVisitor
{
    private readonly string _rel;
    private readonly List<Dictionary<string, object>> _types, _methods, _fields;
    private readonly Dictionary<string, List<int[]>> _bodies;

    public Collector(string rel, List<Dictionary<string, object>> types,
                     List<Dictionary<string, object>> methods,
                     List<Dictionary<string, object>> fields,
                     Dictionary<string, List<int[]>> bodies)
    { _rel = rel; _types = types; _methods = methods; _fields = fields; _bodies = bodies; }

    // The routine's own header line is deliberately NOT included: the declaration itself is
    // in scope, only what is nested inside it is out.
    private void MarkBody(TSqlFragment f)
    {
        if (f == null || f.ScriptTokenStream == null) return;
        var last = f.ScriptTokenStream[f.LastTokenIndex];
        if (!_bodies.TryGetValue(_rel, out var list)) _bodies[_rel] = list = new List<int[]>();
        list.Add(new[] { f.StartLine + 1, last.Line });
    }

    private static string Name(SchemaObjectName n) => n?.BaseIdentifier?.Value;

    private void AddType(string name, string kind, TSqlFragment f)
    {
        if (string.IsNullOrEmpty(name)) return;
        _types.Add(new Dictionary<string, object>
        { ["file"] = _rel, ["name"] = name, ["kind"] = kind, ["line"] = f.StartLine });
    }

    private void AddMethod(string name, string kind, TSqlFragment f)
    {
        if (string.IsNullOrEmpty(name)) return;
        _methods.Add(new Dictionary<string, object>
        { ["file"] = _rel, ["name"] = name, ["kind"] = kind, ["line"] = f.StartLine, ["owner"] = null });
    }

    private void AddColumns(string owner, IList<ColumnDefinition> cols, string kind)
    {
        foreach (var c in cols ?? new List<ColumnDefinition>())
        {
            var cn = c.ColumnIdentifier?.Value;
            if (string.IsNullOrEmpty(cn)) continue;
            _fields.Add(new Dictionary<string, object>
            { ["file"] = _rel, ["name"] = cn, ["owner"] = owner, ["kind"] = kind, ["line"] = c.StartLine });
        }
    }

    public override void Visit(CreateTableStatement n)
    {
        var nm = Name(n.SchemaObjectName);
        AddType(nm, "table", n);
        AddColumns(nm, n.Definition?.ColumnDefinitions, "column");
    }

    // SELECT ... INTO #t declares a table too, but its columns come from the query, so it is
    // excluded for the same reason a view's columns are.
    public override void Visit(CreateViewStatement n) => AddType(Name(n.SchemaObjectName), "view", n);
    public override void Visit(AlterViewStatement n) => AddType(Name(n.SchemaObjectName), "view", n);
    public override void Visit(CreateOrAlterViewStatement n) => AddType(Name(n.SchemaObjectName), "view", n);

    public override void Visit(CreateSequenceStatement n) => AddType(Name(n.Name), "sequence", n);

    public override void Visit(CreateTypeTableStatement n)
    {
        var nm = Name(n.Name);
        AddType(nm, "type", n);
        AddColumns(nm, n.Definition?.ColumnDefinitions, "type_attribute");
    }
    public override void Visit(CreateTypeUddtStatement n) => AddType(Name(n.Name), "type", n);
    public override void Visit(CreateTypeUdtStatement n) => AddType(Name(n.Name), "type", n);

    // A routine's body is NOT descended into, which is how the taxonomy's "temporary objects
    // created inside a routine body are excluded" rule is enforced here. It is not a detail:
    // ScriptDom parses T-SQL procedure bodies in full, and the First Responder Kit declares
    // 407 `CREATE TABLE #scratch` locals inside its 38 procedures. Those are locals, not
    // schema — the same call every other language in this programme makes for a variable
    // declared inside a method body. PostgreSQL gets this for free (libpg_query treats a
    // PL/pgSQL body as an opaque string literal), so not doing it here would have made the
    // T-SQL truth mean something different from the PostgreSQL truth.
    public override void ExplicitVisit(CreateTriggerStatement n) { AddType(Name(n.Name), "trigger", n); MarkBody(n); }
    public override void ExplicitVisit(AlterTriggerStatement n) { AddType(Name(n.Name), "trigger", n); MarkBody(n); }
    public override void ExplicitVisit(CreateOrAlterTriggerStatement n) { AddType(Name(n.Name), "trigger", n); MarkBody(n); }

    public override void ExplicitVisit(CreateProcedureStatement n) { AddMethod(Name(n.ProcedureReference?.Name), "procedure", n); MarkBody(n); }
    public override void ExplicitVisit(AlterProcedureStatement n) { AddMethod(Name(n.ProcedureReference?.Name), "procedure", n); MarkBody(n); }
    public override void ExplicitVisit(CreateOrAlterProcedureStatement n) { AddMethod(Name(n.ProcedureReference?.Name), "procedure", n); MarkBody(n); }

    public override void ExplicitVisit(CreateFunctionStatement n) { AddMethod(Name(n.Name), "function", n); MarkBody(n); }
    public override void ExplicitVisit(AlterFunctionStatement n) { AddMethod(Name(n.Name), "function", n); MarkBody(n); }
    public override void ExplicitVisit(CreateOrAlterFunctionStatement n) { AddMethod(Name(n.Name), "function", n); MarkBody(n); }

    public override void Visit(AlterTableAddTableElementStatement n)
        => AddColumns(Name(n.SchemaObjectName), n.Definition?.ColumnDefinitions, "added_column");
}
