// PRIMARY ground truth for Kotlin — from the Kotlin compiler's own front end (PSI).
//
// Why this exists. Koragraph parses Kotlin with tree-sitter and so does Graphify, so a
// tree-sitter referee would grade two systems that share a grammar — the situation the
// TypeScript section identified and the C# section proved dangerous. The previous handoff
// recorded that "kotlinc is not installed and its PSI is not scriptable the way go/ast is";
// that is half true. kotlinc does not expose an AST-dump flag, but `kotlin-compiler.jar` ships
// the PSI, and building a KtFile from source needs no classpath, no dependency resolution and
// no build configuration — exactly the property that made go/parser and PHP's ext/ast usable
// and clang unusable. This is the compiler's own parse of the file, not a model of it.
//
// Universal Ctags is the second referee (`ast-referee-ctags.py --lang kotlin`).
//
// TAXONOMY — fixed here, before any measurement, and mirrored in that referee's header.
//
//   types      class · interface · enum class · annotation class · data/sealed/value class ·
//              named `object` · `companion object` · `typealias`. A companion with no name is
//              recorded as `Companion`, which IS its name in Kotlin (`Foo.Companion` resolves)
//              and is what ctags reports too.
//   methods    every named `fun` at file, class, interface, object, companion or enum scope,
//              including an abstract signature and an extension function. Secondary
//              constructors are NOT methods — they have no name to match on.
//   fields     properties (`val`/`var`) at type scope, PRIMARY-CONSTRUCTOR properties
//              (`class Point(val x: Int)`), which declare a real property with no other
//              syntax — the rule already applied to TypeScript parameter properties and PHP
//              promoted properties — and enum entries.
//   constants  file-level `val` / `const val` / `var`: the module-constants plane the other
//              languages have.
//
// Excluded, and why: anything inside a function body (a local `fun` or `val` is not a
// declaration of a program entity — every language in this programme except PHP excludes
// function-body bindings, and PHP was the exception only because it hoists them to global
// scope); a plain primary-constructor parameter with no `val`/`var`, which declares no
// property; anonymous objects (`object : Foo() { }`); destructuring declarations; and the
// package and import directives.
//
//   kotlin -cp referee.jar RefereeKt <repoPath> --out truth.json

import com.intellij.openapi.util.Disposer
import com.intellij.psi.PsiComment
import com.intellij.psi.PsiElement
import com.intellij.psi.PsiErrorElement
import com.intellij.psi.PsiWhiteSpace
import org.jetbrains.kotlin.cli.common.messages.MessageCollector
import org.jetbrains.kotlin.cli.jvm.compiler.EnvironmentConfigFiles
import org.jetbrains.kotlin.cli.jvm.compiler.KotlinCoreEnvironment
import org.jetbrains.kotlin.config.CommonConfigurationKeys
import org.jetbrains.kotlin.config.CompilerConfiguration
import org.jetbrains.kotlin.config.KotlinCompilerVersion
import org.jetbrains.kotlin.psi.KtClass
import org.jetbrains.kotlin.psi.KtClassOrObject
import org.jetbrains.kotlin.psi.KtDeclaration
import org.jetbrains.kotlin.psi.KtEnumEntry
import org.jetbrains.kotlin.psi.KtFile
import org.jetbrains.kotlin.psi.KtNamedFunction
import org.jetbrains.kotlin.psi.KtObjectDeclaration
import org.jetbrains.kotlin.psi.KtProperty
import org.jetbrains.kotlin.psi.KtPsiFactory
import org.jetbrains.kotlin.psi.KtScript
import org.jetbrains.kotlin.psi.KtTypeAlias
import java.io.File

private val SKIP = setOf(".git", "node_modules", "target", "build", "vendor", ".gradle", "out")

private data class Decl(
    val file: String, val name: String, val kind: String, val line: Int,
    val endLine: Int? = null, val owner: String? = null, val params: String? = null,
)

private fun esc(s: String): String {
    val sb = StringBuilder()
    for (c in s) when (c) {
        '"' -> sb.append("\\\"")
        '\\' -> sb.append("\\\\")
        '\n' -> sb.append("\\n")
        '\r' -> sb.append("\\r")
        '\t' -> sb.append("\\t")
        else -> if (c < ' ') sb.append("\\u%04x".format(c.code)) else sb.append(c)
    }
    return sb.toString()
}

private fun Decl.toJson(): String {
    val sb = StringBuilder("{\"file\":\"${esc(file)}\",\"name\":\"${esc(name)}\"")
    sb.append(",\"kind\":\"${esc(kind)}\",\"line\":$line")
    endLine?.let { sb.append(",\"end_line\":$it") }
    owner?.let { sb.append(",\"owner\":\"${esc(it)}\"") }
    params?.let { sb.append(",\"params\":\"${esc(it)}\"") }
    return sb.append("}").toString()
}

private class Out {
    val types = mutableListOf<Decl>()
    val methods = mutableListOf<Decl>()
    val fields = mutableListOf<Decl>()
    val constants = mutableListOf<Decl>()
    val errorFiles = mutableListOf<String>()
    var files = 0
    var parseErrors = 0
    var totalBytes = 0L
    var errorBytes = 0L
}

private fun classKind(k: KtClass): String = when {
    k.isInterface() -> "interface"
    k.isEnum() -> "enum"
    k.isAnnotation() -> "annotation"
    k.isData() -> "data_class"
    k.isSealed() -> "sealed_class"
    else -> "class"
}

private fun hasError(e: PsiElement): Boolean {
    if (e is PsiErrorElement) return true
    var c = e.firstChild
    while (c != null) {
        if (hasError(c)) return true
        c = c.nextSibling
    }
    return false
}

// Where a declaration STARTS, on the same convention as every other referee in this programme.
//
// A Kotlin PSI declaration node OWNS its KDoc: `KtNamedFunction.textRange` begins at the `/**`,
// not at `fun`. No other referee here does that. Roslyn scores `n.Span`, which excludes leading
// trivia; `go/parser` returns the keyword position and keeps `Doc` separate; `tsc`'s
// `node.getStart()` skips leading trivia by default; and in tree-sitter a comment is a sibling of
// the declaration, not a child. Left alone, Kotlin — and only Kotlin — would anchor every
// documented declaration at its doc comment while both systems under test report the
// declaration keyword, which shows up as a line-accuracy miss on every documented declaration.
//
// The modifier list is KEPT, including annotations: `method_declaration` in tree-sitter-java
// contains `modifiers`, Roslyn's span contains its attribute lists, and `getStart()` contains
// decorators. Comments are the only thing excluded, because comments are the only thing the
// other four exclude.
private fun declarationStart(e: PsiElement): PsiElement {
    var c = e.firstChild
    while (c != null) {
        val isTrivia = c is PsiComment || c is PsiWhiteSpace
        if (!isTrivia) return c
        c = c.nextSibling
    }
    return e
}

private fun lineOf(file: KtFile, e: PsiElement): Int {
    val text = file.viewProvider.document ?: return 1
    return text.getLineNumber(declarationStart(e).textRange.startOffset) + 1
}

private fun endLineOf(file: KtFile, e: PsiElement): Int {
    val text = file.viewProvider.document ?: return 1
    return text.getLineNumber(e.textRange.endOffset.coerceAtMost(text.textLength)) + 1
}

private fun walk(decls: List<KtDeclaration>, file: KtFile, rel: String, owner: String?, out: Out) {
    for (d in decls) {
        val line = lineOf(file, d)
        when (d) {
            // KtEnumEntry extends KtClass, so it must be matched FIRST or every enum entry
            // would be filed as a nested class.
            is KtEnumEntry -> {
                d.name?.let {
                    out.fields.add(Decl(rel, it, "enum_entry", line, owner = owner))
                }
                walk(d.declarations, file, rel, owner, out)
            }

            is KtClassOrObject -> {
                val name = d.name ?: if (d is KtObjectDeclaration && d.isCompanion()) "Companion" else null
                val kind = when {
                    d is KtObjectDeclaration && d.isCompanion() -> "companion_object"
                    d is KtObjectDeclaration -> "object"
                    d is KtClass -> classKind(d)
                    else -> "class"
                }
                if (name != null) {
                    out.types.add(Decl(rel, name, kind, line, endLine = endLineOf(file, d)))
                }
                // `class Point(val x: Int)` declares a real property; a plain `(name: String)`
                // parameter declares nothing.
                for (p in d.primaryConstructorParameters) {
                    if (!p.hasValOrVar()) continue
                    p.name?.let {
                        out.fields.add(Decl(rel, it, "constructor_property", lineOf(file, p), owner = name))
                    }
                }
                walk(d.declarations, file, rel, name ?: owner, out)
            }

            is KtNamedFunction -> {
                d.name?.let {
                    out.methods.add(Decl(
                        rel, it, if (owner == null) "function" else "method", line,
                        endLine = endLineOf(file, d), owner = owner,
                        params = d.valueParameterList?.text?.trim('(', ')')?.replace(Regex("\\s+"), " "),
                    ))
                }
                // Deliberately NOT descending into the body: a local `fun` or `val` is not a
                // declaration of a program entity.
            }

            is KtProperty -> {
                d.name?.let {
                    if (owner == null) {
                        out.constants.add(Decl(rel, it, if (d.hasModifier(
                                org.jetbrains.kotlin.lexer.KtTokens.CONST_KEYWORD)) "const" else "val", line))
                    } else {
                        out.fields.add(Decl(rel, it, "property", line, owner = owner))
                    }
                }
            }

            is KtTypeAlias -> {
                d.name?.let { out.types.add(Decl(rel, it, "typealias", line, endLine = endLineOf(file, d))) }
            }

            // A `.kts` script's contents are wrapped in a KtScript, so `KtFile.declarations`
            // returns the script itself and everything inside it is invisible unless the script
            // is descended into: without this, a `.kts` file yields no truth at all and every
            // real declaration in a build script scores as a fabrication.
            is KtScript -> walk(d.declarations, file, rel, owner, out)

            else -> { /* secondary constructors, initialisers, destructuring — no name */ }
        }
    }
}

fun main(args: Array<String>) {
    val repo = args[0]
    val outPath = args.toList().zipWithNext().firstOrNull { it.first == "--out" }?.second

    val disposable = Disposer.newDisposable()
    val config = CompilerConfiguration()
    config.put(CommonConfigurationKeys.MODULE_NAME, "referee")
    config.put(CommonConfigurationKeys.MESSAGE_COLLECTOR_KEY, MessageCollector.NONE)
    val env = KotlinCoreEnvironment.createForProduction(
        disposable, config, EnvironmentConfigFiles.JVM_CONFIG_FILES)
    val factory = KtPsiFactory(env.project, markGenerated = false)

    val out = Out()
    val root = File(repo)
    val paths = root.walkTopDown()
        .onEnter { it.name !in SKIP }
        .filter { it.isFile && (it.name.endsWith(".kt") || it.name.endsWith(".kts")) }
        .sortedBy { it.path }
        .toList()

    for (f in paths) {
        // IntelliJ's DocumentImpl asserts on CRLF ("Wrong line separators"), and detekt ships
        // Windows-line-ending fixtures — the referee died mid-corpus on the fourth repository.
        // Normalising to LF preserves the line COUNT, so every reported line number is
        // unchanged; only the separator differs.
        val text = try { f.readText().replace("\r\n", "\n").replace('\r', '\n') }
            catch (e: Exception) { continue }
        val rel = f.path.removePrefix(root.path).removePrefix(File.separator)
        out.files++
        out.totalBytes += text.length
        val ktFile = try { factory.createFile(f.name, text) } catch (e: Throwable) {
            out.parseErrors++; out.errorBytes += text.length; out.errorFiles.add(rel); continue
        }
        if (hasError(ktFile)) {
            out.parseErrors++
            out.errorBytes += text.length
            out.errorFiles.add(rel)
        }
        walk(ktFile.declarations, ktFile, rel, null, out)
    }

    val kinds = sortedMapOf<String, Int>()
    for ((plane, list) in listOf("types" to out.types, "methods" to out.methods,
                                 "fields" to out.fields, "constants" to out.constants)) {
        for (d in list) kinds["$plane:${d.kind}"] = (kinds["$plane:${d.kind}"] ?: 0) + 1
    }
    val errPct = if (out.totalBytes > 0)
        Math.round(10000.0 * out.errorBytes / out.totalBytes) / 100.0 else 0.0

    val json = StringBuilder()
    json.append("{\"repo\":\"${esc(repo)}\",\"lang\":\"kotlin\",")
    json.append("\"referee\":\"kotlin compiler PSI (kotlin-compiler.jar)\",")
    // The PSI is the compiler's own parse tree, so the compiler jar on the classpath IS the
    // grammar that produced this truth set. Kotlin's parser changes between minor releases, so
    // the version is recorded in the truth file as provenance.
    json.append("\"referee_version\":\"kotlin-compiler ${esc(KotlinCompilerVersion.VERSION)}" +
        " / jvm ${esc(System.getProperty("java.version") ?: "unknown")}\",")
    json.append("\"files\":${out.files},\"parse_errors\":${out.parseErrors},")
    json.append("\"error_files\":[${out.errorFiles.joinToString(",") { "\"${esc(it)}\"" }}],")
    json.append("\"disabled_ranges\":{},")
    for ((plane, list) in listOf("types" to out.types, "methods" to out.methods,
                                 "fields" to out.fields, "constants" to out.constants)) {
        json.append("\"$plane\":[${list.joinToString(",") { it.toJson() }}],")
    }
    json.append("\"counts\":{\"files\":${out.files},\"types\":${out.types.size},")
    json.append("\"methods\":${out.methods.size},\"fields\":${out.fields.size},")
    json.append("\"constants\":${out.constants.size},\"parse_errors\":${out.parseErrors},")
    json.append("\"error_byte_pct\":$errPct},")
    json.append("\"kind_breakdown\":{${kinds.entries.joinToString(",") { "\"${esc(it.key)}\":${it.value}" }}}}")

    outPath?.let { File(it).writeText(json.toString()) }
    println("{\"counts\":{\"files\":${out.files},\"types\":${out.types.size},\"methods\":${out.methods.size}," +
        "\"fields\":${out.fields.size},\"constants\":${out.constants.size}," +
        "\"parse_errors\":${out.parseErrors},\"error_byte_pct\":$errPct}," +
        "\"kinds\":{${kinds.entries.joinToString(",") { "\"${esc(it.key)}\":${it.value}" }}}}")
    Disposer.dispose(disposable)
    System.exit(0)
}
