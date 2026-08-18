// Second, independent ground truth for C# — from Roslyn, the C# compiler's own parser.
//
// Why this exists. Both systems under test parse C# with tree-sitter, so a tree-sitter referee
// shares a grammar lineage with them. It is also unreliable on large files: `tree_sitter_c_sharp`
// 0.23.5 reports a parse error on a substantial share of a big C# corpus and under-reports the
// declarations in those files, which scores correct extractions as false positives. Roslyn is
// the definition of what a C# declaration is, and neither system under test uses it.
//
// Emits the same JSON shape as ast-referee.py, implementing the same inclusion rules in the
// compiler's terms.
//
//   dotnet run -c Release --project referees/ast-referee-roslyn -- <repo> --out t.json

using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

var skipDirs = new HashSet<string>
{
    ".git", "bin", "obj", "packages", "artifacts", "node_modules", "vendor", "target", "build", "out"
};

var repo = args[0];

// Roslyn parses ONE compilation configuration: text inside an `#if SYMBOL` block whose symbol
// is undefined becomes disabled trivia and is never parsed. tree-sitter parses every branch.
// That is a genuine difference in what "the declarations in this source" means, and it is not
// small: a library can gate whole files behind a single `#if SYMBOL`.
//
// Defining every symbol found in any `#if` is not a fix — it flips `#if !SYMBOL` branches off
// instead, hiding a different set of declarations. Roslyn has no "parse all branches" mode.
//
// So the referee parses the default configuration and additionally reports the line ranges it
// could NOT adjudicate. The comparison excludes those ranges from BOTH sides: a declaration
// inside a preprocessor-disabled region is real, but no referee here can judge it, and scoring
// a system's recovery of it as a false positive would measure the referee's blind spot.
string outPath = null;
var outIdx = Array.IndexOf(args, "--out");
if (outIdx >= 0 && outIdx + 1 < args.Length) outPath = args[outIdx + 1];

var types = new List<Dictionary<string, object>>();
var methods = new List<Dictionary<string, object>>();
var fields = new List<Dictionary<string, object>>();
var constants = new List<Dictionary<string, object>>();
int fileCount = 0, parseErrors = 0;
var currentRel = "";
var disabledRanges = new Dictionary<string, List<int[]>>();
SyntaxTree currentTree = null;

foreach (var path in EnumerateSources(repo))
{
    fileCount++;
    string text;
    try { text = File.ReadAllText(path); } catch { continue; }
    currentTree = CSharpSyntaxTree.ParseText(text, new CSharpParseOptions(LanguageVersion.Preview));
    if (currentTree.GetDiagnostics().Any(d => d.Severity == DiagnosticSeverity.Error)) parseErrors++;
    currentRel = Path.GetRelativePath(repo, path);
    RecordDisabledRanges(currentTree.GetRoot());
    Collect(currentTree.GetRoot());
}

var counts = new Dictionary<string, object>
{
    ["files"] = fileCount,
    ["types"] = types.Count,
    ["methods"] = methods.Count,
    ["fields"] = fields.Count,
    ["constants"] = constants.Count,
    ["parse_errors"] = parseErrors,
};

var kinds = new SortedDictionary<string, int>();
CountKinds("types", types);
CountKinds("methods", methods);
CountKinds("fields", fields);
CountKinds("constants", constants);

var payload = new Dictionary<string, object>
{
    ["repo"] = repo,
    ["lang"] = "csharp",
    ["referee"] = "roslyn",
    // The Microsoft.CodeAnalysis.CSharp assembly that parsed this corpus. Roslyn's syntax model
    // changes with the C# language version it ships, so a truth set without this carries no
    // provenance: a reader on a different SDK could not tell whether a disagreement comes from
    // their compiler or from extraction.
    ["referee_version"] = "Microsoft.CodeAnalysis.CSharp "
        + (typeof(Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree).Assembly
             .GetName().Version?.ToString() ?? "unknown")
        + $" / dotnet {Environment.Version}",
    ["files"] = fileCount,
    ["parse_errors"] = parseErrors,
    ["types"] = types,
    ["methods"] = methods,
    ["fields"] = fields,
    ["constants"] = constants,
    ["counts"] = counts,
    ["kind_breakdown"] = kinds,
    ["disabled_ranges"] = disabledRanges,
};

if (outPath != null) File.WriteAllText(outPath, JsonSerializer.Serialize(payload));
Console.WriteLine(JsonSerializer.Serialize(
    new Dictionary<string, object> { ["counts"] = counts, ["kinds"] = kinds },
    new JsonSerializerOptions { WriteIndented = true }));

void CountKinds(string plane, List<Dictionary<string, object>> list)
{
    foreach (var item in list)
        if (item.TryGetValue("kind", out var k) && k is string ks)
        {
            var key = plane + ":" + ks;
            kinds[key] = kinds.TryGetValue(key, out var c) ? c + 1 : 1;
        }
}

void RecordDisabledRanges(SyntaxNode root)
{
    List<int[]> ranges = null;
    foreach (var trivia in root.DescendantTrivia())
    {
        if (!trivia.IsKind(SyntaxKind.DisabledTextTrivia)) continue;
        var span = currentTree.GetLineSpan(trivia.Span);
        (ranges ??= new List<int[]>()).Add(new[]
        {
            span.StartLinePosition.Line + 1, span.EndLinePosition.Line + 1,
        });
    }
    if (ranges != null) disabledRanges[currentRel] = ranges;
}

IEnumerable<string> EnumerateSources(string root)
{
    var stack = new Stack<string>();
    stack.Push(root);
    while (stack.Count > 0)
    {
        var dir = stack.Pop();
        string[] subs, files;
        try { subs = Directory.GetDirectories(dir); files = Directory.GetFiles(dir, "*.cs"); }
        catch { continue; }
        foreach (var sub in subs)
            if (!skipDirs.Contains(Path.GetFileName(sub))) stack.Push(sub);
        foreach (var f in files) yield return f;
    }
}

int LineOf(SyntaxNode n) => currentTree.GetLineSpan(n.Span).StartLinePosition.Line + 1;
int EndLineOf(SyntaxNode n) => currentTree.GetLineSpan(n.Span).EndLinePosition.Line + 1;

string OwnerOf(SyntaxNode n)
{
    for (var cur = n.Parent; cur != null; cur = cur.Parent)
    {
        if (cur is BaseTypeDeclarationSyntax bt) return bt.Identifier.ValueText;
        if (cur is DelegateDeclarationSyntax d) return d.Identifier.ValueText;
    }
    return null;
}

string Normalise(string s) =>
    string.Join(" ", (s ?? "").Trim('(', ')').Split((char[])null, StringSplitOptions.RemoveEmptyEntries));

void AddType(SyntaxNode n, string name, string kind) => types.Add(new Dictionary<string, object>
{
    ["file"] = currentRel, ["name"] = name, ["kind"] = kind,
    ["line"] = LineOf(n), ["end_line"] = EndLineOf(n),
});

void AddMethod(SyntaxNode n, string name, string kind, string parameters) => methods.Add(new Dictionary<string, object>
{
    ["file"] = currentRel, ["name"] = name, ["kind"] = kind, ["owner"] = OwnerOf(n),
    ["params"] = Normalise(parameters),
    ["line"] = LineOf(n), ["end_line"] = EndLineOf(n),
});

void AddField(SyntaxNode n, string name, string kind, string type) => fields.Add(new Dictionary<string, object>
{
    ["file"] = currentRel, ["name"] = name, ["kind"] = kind, ["owner"] = OwnerOf(n),
    ["type"] = type, ["line"] = LineOf(n),
});

void Collect(SyntaxNode root)
{
    foreach (var n in root.DescendantNodesAndSelf())
    {
        switch (n)
        {
            // Types. RecordDeclarationSyntax covers `record` and `record struct`; its positional
            // parameters declare real members with no other declaring syntax — the same case as
            // TypeScript's constructor parameter properties. Checked BEFORE class/struct because
            // RecordDeclarationSyntax derives from TypeDeclarationSyntax.
            case RecordDeclarationSyntax r:
                AddType(r, r.Identifier.ValueText, "record");
                if (r.ParameterList != null)
                    foreach (var p in r.ParameterList.Parameters)
                        AddField(p, p.Identifier.ValueText, "record_parameter", p.Type?.ToString());
                break;
            case ClassDeclarationSyntax c: AddType(c, c.Identifier.ValueText, "class"); break;
            case InterfaceDeclarationSyntax i: AddType(i, i.Identifier.ValueText, "interface"); break;
            case StructDeclarationSyntax st: AddType(st, st.Identifier.ValueText, "struct"); break;
            case EnumDeclarationSyntax e: AddType(e, e.Identifier.ValueText, "enum"); break;
            case DelegateDeclarationSyntax d: AddType(d, d.Identifier.ValueText, "delegate"); break;

            // Callables. A property accessor is NOT a method: a property is one member, and
            // `{ get; set; }` usually declares no body at all.
            case MethodDeclarationSyntax m:
                AddMethod(m, m.Identifier.ValueText, "method", m.ParameterList?.ToString()); break;
            case ConstructorDeclarationSyntax ct:
                AddMethod(ct, ct.Identifier.ValueText, "constructor", ct.ParameterList?.ToString()); break;
            case DestructorDeclarationSyntax de:
                AddMethod(de, de.Identifier.ValueText, "destructor", de.ParameterList?.ToString()); break;
            case OperatorDeclarationSyntax op:
                AddMethod(op, "operator" + op.OperatorToken.ValueText, "operator", op.ParameterList?.ToString()); break;
            case ConversionOperatorDeclarationSyntax co:
                AddMethod(co, "operator" + co.Type, "operator", co.ParameterList?.ToString()); break;
            case LocalFunctionStatementSyntax lf:
                AddMethod(lf, lf.Identifier.ValueText, "local_function", lf.ParameterList?.ToString()); break;

            // Fields. One declaration can declare several names: `string _a, _b;` is two.
            case FieldDeclarationSyntax f:
                foreach (var v in f.Declaration.Variables)
                    AddField(f, v.Identifier.ValueText, "field", f.Declaration.Type?.ToString());
                break;
            case EventFieldDeclarationSyntax ev:
                foreach (var v in ev.Declaration.Variables)
                    AddField(ev, v.Identifier.ValueText, "event", ev.Declaration.Type?.ToString());
                break;
            case PropertyDeclarationSyntax pr:
                AddField(pr, pr.Identifier.ValueText, "property", pr.Type?.ToString()); break;
            case EnumMemberDeclarationSyntax em:
                AddField(em, em.Identifier.ValueText, "enum_member", null); break;

            // Deliberately excluded: indexers (no stable name to match on).
        }
    }
}
