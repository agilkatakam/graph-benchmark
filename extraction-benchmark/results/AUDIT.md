# Adversarial audit record

Before publishing, three independent reviewers were run over the harness with one instruction:
**act as Graphify's expert witness and find every way this benchmark is unfair to it.** Not to
confirm the result — to break it. Every claim had to carry a number produced by running code;
"this probably causes X" was rejected.

They found **eleven defects. Every one ran in Koragraph's favour.** All eleven are fixed, plus two
more of the same species found afterwards on the held-out corpus (#12, #13), and a fourteenth
(#14) that ran against **both** systems and was found by publishing a measure the competitor was
winning. This file records what they were, so a reader can check the fix rather than take the
result on trust.

The standing rule that generated most of them: **if the competitor's precision looks implausibly
bad, the bug is almost certainly ours.** It fired fourteen times across this programme.

---

## Fixed — the harness was wrong

| # | language | defect | measured effect on Graphify |
|---|---|---|---|
| 1 | SQL | Its `alter_table` branch creates a node for the altered object so its `references` edge has an endpoint. A container, not a declaration — the same species as the C# `namespace` node the harness already excluded. | type precision **93.83% → 97.93%**; 196 of its 367 SQL fabrications |
| 2 | Go | Receiver-owner stubs: for `func (c *Command) Foo()` in a file that does not declare `Command`, it creates a `Command` node to hang the `method` edge on. The container rule keys on a `type` field that Go nodes do not carry, so it never fired. | type precision **93.79% → 99.08%**; 456 of 457 unmatched non-callables |
| 3 | Kotlin | Its enum entries (`case_of` edges) were decoded as types. The Java decoder had routed `case_of` to the field plane since the Java section was written; the shared TS/Go/PHP/Kotlin decoder never did. | type precision **92.26% → 99.96%**, and it *has* a field plane — 812 enum entries at 99.88% precision, where the section claimed it had none |
| 4 | C/C++ | Its `field_declaration` branch emits a node for member-function *prototypes* too, and its C++ labels carry no `()` to tell them apart, so 377 correct methods were filed as fabricated fields. | field precision **85.16% → 96.62%** |
| 5 | C/C++ | Forward declarations (`struct redisDb;`) — it emits a node, ctags does not tag one, truth has no entry. | 171 type-plane fabrications removed; type precision **83.01% → 96.54%** |
| 6 | C/C++ | Template specializations spelled with their argument list (`BuiltInDefaultValue<const T>`) by Graphify and bare by ctags and Koragraph. `norm_cpp` normalised `operator` spacing only. | 120 correct extractions restored |
| 7 | TS/JS | `namespace`, `declare module` and `export * as` container nodes charged as fabricated types — same defect as #1/#2, third encoding. | 113 fabrications removed |
| 8 | Python | `.pyi` stubs are not in its `CODE_EXTENSIONS`, so it never opens one; it was charged recall for 1,278 declarations in pandas. | method recall **92.52% → 93.65%** |
| 9 | Python | The non-callable row pooled types **and** constants, on the stated premise that its non-callable bucket holds both. Measured, that bucket holds 11,748 types and **4** constants — so 6,472 unreachable rows were added to its denominator. | type recall **52.02% → 75.23%** |
| 10 | C/C++ | `disabled_ranges` filtered its method and type planes but not its field plane. | 32 uncounterable fabrications removed |
| 11 | C/C++, Kotlin | `norm_cpp` / `norm_kotlin` normalised its method and type planes but not its field plane. | latent; became live once #3 and #4 landed |
| 12 | C/C++ | `namespace` container nodes — the same defect as #1, #2, #5 and #7 in a fifth encoding, found on the held-out corpus. | held-out method precision **82.10% → 94.93%** |
| 13 | C/C++ | Out-of-class member definitions (`void parse_context::do_check_arg_id(...)`) are labelled with the qualification by Graphify and with the bare member name by ctags and Koragraph; `norm_cpp` did not drop it. | 79 correct extractions restored; held-out method recall **35.33% → 40.82%** |

**After all eleven, Graphify's precision is 93%–100% on every plane of every language on both
corpora**, against published figures as low as 25% before the audit. Excluding C/C++ every cell
is between 97.93% and 100.00%; C/C++ is **93.02%–98.93%** — that corpus is four
template- and macro-heavy header libraries, and its residual fabrications there are its own
(error-recovery labels that run to a whole class body). Its extraction is essentially as
*correct* as Koragraph's. The difference that survives is **coverage**, which is what the tables
report.

---

## A fourteenth: a referee defect that ran against BOTH systems

Every entry above ran in Koragraph's favour, because every one was found by asking what the
harness did to *Graphify* unfairly. Publishing the line-accuracy measure (§3.2 of the paper
section) asked the opposite question and turned up one that ran the other way.

| # | language | defect | measured effect |
|---|---|---|---|
| 14 | Kotlin | A Kotlin PSI declaration node **owns its KDoc**: `KtNamedFunction.textRange` begins at `/**`, not at `fun`. Roslyn's `Span`, `go/parser`'s `Pos()`, `tsc`'s `getStart()` and tree-sitter all exclude a leading doc comment, so Kotlin — and only Kotlin — anchored every documented declaration on its comment while both systems under test report the keyword. | **5,286 of 5,309** Kotlin line-accuracy misses (99.6%). Koragraph 87.16% → **99.94%**, Graphify 88.61% → **99.95%** on the development corpus; held-out 88.32% → 99.98% and 90.73% → 99.90%. Recall and precision are unaffected — matching is on `(file, name)`. |

It is recorded here rather than quietly fixed because the honest reading cuts against the
framing of the section above. "Every defect ran in Koragraph's favour" was true of the eleven the
adversarial reviewers found, and it stopped being true the moment a measure the competitor was
winning got published. The published Kotlin line-accuracy figures were penalising the competitor
by 11.3 points and us by 12.8.

## Extractor defects the same measure found — ours, not the harness's

Not audit findings about fairness; product defects, recorded because §3.2 of the paper attributes
its own movement to them.

| language | defect | measured effect |
|---|---|---|
| Java | The declaration-start scan required an annotation to be a whole line whose argument list contained no `)`. `@InlineMe(replacement = "this.convert(a)")` stopped the walk, so the node anchored on the signature while truth anchored on the modifier list. | Held-out Java line-accuracy misses 749 → 339; guava alone 597 → 182. Java held-out line accuracy 99.36% → **99.71%**, development 99.80% → 99.87%. |
| C# | On a degraded parse the grammar walk and the regex scanner are merged and deduped within a **±2-line window** — which any declaration carrying three or more attribute lines falls outside. `JsonConvert.SerializeObject` was emitted twice, once at `[DebuggerStepThrough]` and once at the signature. A fabrication and a wrong line at once. Dedupe is by span containment now. | With the continuation-line guard below, **591 method nodes** removed from the development corpus — recall unchanged at 99.90%, precision 99.51% → **99.81%**, line accuracy 99.42% → 99.79%. Recall holding flat while precision rises is what says they were fabrications. |
| C# | A method CALL on a line beginning with a continuation operator (`? CanSetForeignKey(`) matched the declaration pattern. The containment dedupe cannot catch the ones that call a *different* method. | Folded into the 538 above; the remaining shadow nodes in `InternalForeignKeyBuilder.cs` went 5 → 0. Cost: 4 declarations of C/C++ held-out recall (96.62% → 96.59%) where a removed duplicate had been filling a multiset slot, against +0.10 of precision. |

---

## Also changed — methodology, against ourselves

**The TypeScript and JavaScript referee.** `ast-referee.py`'s TS/JS collectors were written from
Koragraph's own extractor: the same node-type sets, the same branch structure, in places the same
comments. Grading Koragraph with it was close to tautological, which is what the 100.00%/100.00%
cells were. The primary referee is now the **TypeScript compiler**, which neither system uses —
the same rule already applied to Go, PHP, C#, Kotlin and Python. Koragraph's own numbers fell,
and two real gaps surfaced (99 ambient `declare const` constants, since fixed; 68 Flow type-alias
members in gatsby, disclosed below).

**The Python referee.** Switched from tree-sitter to **CPython's `ast`** for the same reason, and
its taxonomy was corrected at the same time: binding scope, not syntactic nesting, decides the
plane (`if TYPE_CHECKING:` at module level binds a module name), and every element of a tuple or
starred target is a separate binding. That added 398 constants and 215 attributes to truth on
both sides.

---

## Disclosed, not corrected — these are methodology, and the reader should weigh them

**Multiset matching.** `(file, name)` multiset: an overload one side collapses counts as a recall
miss. Graphify's node id is `(scope, name)` with no parameter list, so same-named declarations in
a scope collapse **by design**. Measured, set-semantics instead of multiset moves its method
recall: Java 84.00 → 96.92, TypeScript 70.26 → 84.44, JavaScript 66.42 → 75.44, Kotlin 88.28 →
96.06, C/C++ 57.78 → 66.07. Koragraph moves under 0.1 points in four of the five languages and
0.68 in C/C++, because it emits one node per declaration site almost everywhere.
Regenerate with `harness/ast-sensitivity.py --knob multiset` rather than trusting this copy. An overload *is* a distinct declaration and the multiset is the honest primary,
but the gap between the two columns is the size of a node-identity modelling difference, not of
an extraction difference, and both are reported.

**Declarations inside function bodies (TS/JS).** Graphify's walk does not enter a
`statement_block` to emit nodes; its own source cites its issue #1077. The taxonomy has no depth
filter, so it is charged for a scope it is documented not to enter. Restricting truth to
top-scope declarations moves its method recall from 70.26% to 90.94% (TypeScript) and 66.42% to
88.88% (JavaScript). On lodash, 95.6% of truth methods sit in a scope it never emits into. The
TypeScript-compiler referee now stamps `in_function_body` on every declaration, so this is
computed rather than asserted: `harness/ast-sensitivity.py --knob bodyscope`.

**Corpus composition.** The fraction of truth methods inside a function body is lodash 95.6%,
gatsby 65.8%, eslint 60.8%, vue-core 66.3%, express 57.4% — against webpack 19.4%. Whether or not
the selection was deliberate, a reviewer will compute this, so it is stated here; the full
per-repository table is `harness/ast-sensitivity.py --knob composition`.

**The C/C++ macro-line exclusion.** Declarations whose name collides with a macro name anywhere in
the repo are excluded from both systems. Measured (`harness/ast-sensitivity.py --knob macro`):
without it, Koragraph's C/C++ method precision falls from 98.02% to 87.97% and Graphify's from
98.49% to 93.58%. It is worth **+10.05 points to us and +4.91 to Graphify**, and **Graphify leads
this plane in both conditions**. An earlier version of this paragraph said the exclusion made
"the precision winner flip". It does not — Graphify was already ahead — and the correct reading
is less flattering to us than the one it replaced.

**The C/C++ referee is the weakest in the set.** Universal Ctags is a hand-written tagger, not a
compiler front end; the second referee is the grammar family both systems use. They disagree on
**18.9%** of the method plane. Swapping the referee moves Koragraph's figures by −3.5 to +1.6
points and Graphify's by −4.8 to +0.6, and does not change who leads on any plane or either
measure. Both are published; regenerate with `harness/ast-second-referee.sh --lang cpp`.

**Known Koragraph gaps.** JavaScript field recall is 68.66% on a 217-declaration plane; all 68
misses are Flow type-alias members in gatsby, which is Flow, not JavaScript, and which the
JavaScript grammar does not model. Kotlin is the one grammar-limited language: the grammar the
extractor loads (`tree-sitter-wasms@0.1.13`) errors on **8.02% of files and 20.9% of bytes** of
that corpus. The build has to be named — the Python `tree-sitter-kotlin` 0.23 bindings give
3.19% / 8.2% on the same files, and an earlier version of this line gave a third pair of numbers
without saying which grammar produced them.

---

## One reviewer claim I could not confirm

An audit reported that the SQL referee's schema-stripping merges distinct objects
(`columnar_internal.options` vs `columnar.options`) and charges ~122 fabrications. Measured: the
dedupe drops 1,614 entries across the SQL corpus, of which **3** involve genuinely distinct
qualified names, and two of those are case-only variants that SQL treats as the same unquoted
identifier. The rule is collapsing genuine re-declarations of the same object, which is what it
is for. Not changed.

A second reported the container-node defect as programme-wide. It is C#-only in that form:
Java, Python and Go `package` nodes have `pom.xml`-style source files and were already excluded
by the extension filter. The same *species* of defect did turn out to exist in four other
encodings (#1, #2, #5, #7), which is why they are listed separately.
