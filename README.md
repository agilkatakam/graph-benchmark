# Koragraph vs Graphify — benchmarks

Reproducible benchmarks against **Graphify v0.9.28**.

- **[RESULTS.md](RESULTS.md)** — every measured number, both benchmarks, including the ones we lose.
- **[extraction-benchmark/](extraction-benchmark/)** — declaration extraction, 100 repositories, 10 languages, refereed by each language's own compiler front end.
- **[retrieval-benchmark/](retrieval-benchmark/)** — end-to-end retrieval, 4 repositories, 401 questions.

Koragraph is closed source. **Graphify's column is reproducible from this repository** — pinned
corpus SHAs, the commands, the scorer, and the questions are all here, at every traversal depth
from 2 to 6. Verified from a clean environment: 2406/2406 Graphify context cells byte-identical.
Koragraph's column can be re-scored from the shipped contexts but not re-run.

Each benchmark's `results/AUDIT.md` records the methodology decisions and the limits that bear on
how its numbers should be read, including every finding that ran against us.

## What changed in this revision

The previous revision of the retrieval benchmark is superseded. Five corrections, each verified by
re-running the harness:

1. **Graphify was pinned to traversal depth 2.** Its CLI hardcodes that (`cli.py:949`), but its own
   function and MCP tool default to 3, with a documented maximum of 6. Depth is the largest single
   variable in this benchmark and the previous headline overstated the gap by roughly 3×. Graphify
   is now compared at depth 6; both arms ship.
2. **The Koragraph column was superseded.** The published column was flat from budget 2000 upward
   because retrieval breadth ignored the caller's budget. That was a product defect, since fixed.
3. **The zlib corpus table was wrong** — 37 files / 23,301 lines, silently excluding `contrib/`,
   where 17 of 36 distinct gold files live. It is 63 / 36,170.
4. **The stated cause of the gap was wrong.** Coverage of declaration planes accounts for 21.9% of
   Graphify's misses, not the bulk of them. This repository's own shipped graphs disprove the
   earlier claim.
5. **The reproduction command failed.** `score.py` resolves artifacts by question-file stem, so the
   documented `--out /tmp/my-viper.json` skipped every cell and exited FATAL. Corrected.

A claim that a competitor can falsify in an afternoon is worth less than no claim at all. Where
Graphify wins — pooled extraction precision, line accuracy in 7 of 20 cells, non-macro C retrieval
at two budgets — it is reported as a win.
