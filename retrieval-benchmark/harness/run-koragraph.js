#!/usr/bin/env node
// Run every question against Koragraph at every budget, and record the returned context.
//
// FIDELITY
// --------
// This uses the product's own retrieval and the product's own serializer, not a
// benchmark-special path:
//
//   graph-retriever.js#retrieveSubgraph()  — the live hybrid vector+graph retrieval, the same
//                                            call Iris, Kora and /api/v1/graph/search make
//   subgraph-builder.js#formatSubgraph()   — the live serializer that turns a retrieved
//                                            subgraph into the text a consuming model reads
//
// Nothing here reformats, re-ranks or enriches beyond what a user of the product would get.
// If the shipped format loses, that is a product finding, and it gets fixed in the product.
//
// BUDGET PARITY
// -------------
// Graphify cuts its context at `token_budget * 3` characters (serve.py:802) and cuts on a line
// boundary. The identical rule is applied here, using THEIR constant — which is stricter than a
// real tokenizer on code, so it hands Koragraph less context per nominal token, not more. Actual
// characters and actual tokens are both recorded so the paper can report measured tokens rather
// than either side's approximation.
//
// Usage:
//   node run-koragraph.js --questions <q.json> --project-id N --out <ctx.json>
//                         [--budgets 500,1000,2000,4000,8000]

'use strict';
require('dotenv').config({ path: require('path').join(__dirname, '../../koragraph_api/.env'), override: true });

const fs = require('fs');
const path = require('path');
const API = path.join(__dirname, '../../koragraph_api');
const pool = require(path.join(API, 'src/db/pool'));
const { retrieveSubgraph } = require(path.join(API, 'src/services/graph-retriever'));
const { formatSubgraph } = require(path.join(API, 'src/services/subgraph-builder'));

const CHARS_PER_TOKEN = 3; // Graphify's constant, adopted verbatim for budget parity.

function parseArgs() {
  const a = process.argv.slice(2);
  const o = { budgets: '500,1000,2000,4000,8000' };
  for (let i = 0; i < a.length; i++) {
    if (a[i] === '--questions') o.questions = a[++i];
    else if (a[i] === '--project-id') o.projectId = parseInt(a[++i], 10);
    else if (a[i] === '--out') o.out = a[++i];
    else if (a[i] === '--budgets') o.budgets = a[++i];
  }
  if (!o.questions || !o.projectId || !o.out) {
    console.error('Usage: run-koragraph.js --questions <q.json> --project-id N --out <ctx.json>');
    process.exit(1);
  }
  return o;
}

// Same cut as _subgraph_to_text: last line boundary at or before the char budget.
function cutToBudget(text, budget) {
  const charBudget = budget * CHARS_PER_TOKEN;
  if (text.length <= charBudget) return text;
  const slice = text.slice(0, charBudget);
  const nl = slice.lastIndexOf('\n');
  return slice.slice(0, nl > 0 ? nl : charBudget);
}

async function main() {
  const opts = parseArgs();
  const qs = JSON.parse(fs.readFileSync(opts.questions, 'utf8'));
  const budgets = opts.budgets.split(',').map(Number);

  const { rows: branchRows } = await pool.query(
    `SELECT rb.id FROM koragraph.repository_branches rb
       JOIN koragraph.repositories r ON r.id = rb.repository_id
      WHERE r.project_id = $1`, [opts.projectId]);
  const branchIds = branchRows.map(r => r.id);
  if (!branchIds.length) throw new Error(`FATAL: project ${opts.projectId} has no tracked branches — ingest first`);

  // Layer-3 assertion: a silently-down purpose server produces the same graph minus purposes,
  // with no error, and would make this run quietly different from the shipped product.
  const { rows: [counts] } = await pool.query(
    `SELECT count(*)::int AS nodes,
            count(*) FILTER (WHERE n.properties ? 'purpose')::int AS purposes
       FROM koragraph.nodes n WHERE n.repository_branch_id = ANY($1::bigint[])`, [branchIds]);
  if (!counts.nodes) throw new Error(`FATAL: project ${opts.projectId} has zero nodes`);
  if (!counts.purposes) {
    throw new Error(`FATAL: project ${opts.projectId} has ${counts.nodes} nodes but ZERO purposes. ` +
      `The purpose server (:8091) was down during ingest. Re-ingest — do not measure this.`);
  }

  const embedHealth = await fetch('http://localhost:8089/health').then(r => r.json()).catch(() => null);
  if (!embedHealth) throw new Error('FATAL: embedding server :8089 unreachable — retrieval cannot run');

  // Slice 2 (PLAN_KORAGRAPH_VS_GITNEXUS.md): retrieval breadth is now budget-aware
  // (graph-retriever.js#deriveMaxNodesFromBudget), so a single retrieval-then-truncate no longer
  // represents what a real consumer gets — a caller offering 32000 tokens should ask retrieval
  // for a wider subgraph, not just receive a bigger slice of the same 25-node one. Retrieve once
  // per budget, passing the cell's budget the same way MCP compile_context / Iris / Kora would.
  // cutToBudget stays as the final safety cut in case the char estimate overshoots.
  const results = [];
  for (const q of qs.questions) {
    const t0 = process.hrtime.bigint();
    const c0 = process.cpuUsage();

    const perBudget = {};
    let maxFullChars = 0;
    let maxRetrievedNodes = 0;
    let maxRetrievedEdges = 0;
    for (const b of budgets) {
      const sub = await retrieveSubgraph(q.question, branchIds, [], { projectId: opts.projectId, budget: b });
      const full = formatSubgraph(sub.nodes || [], sub.edges || [], null);
      if (!full || !full.trim()) {
        throw new Error(`FATAL: Koragraph returned empty context for ${q.id} at budget ${b}. ` +
          `Refusing to record an empty result as a measurement.`);
      }
      const text = cutToBudget(full, b);
      perBudget[String(b)] = { context: text, chars: text.length };
      maxFullChars = Math.max(maxFullChars, full.length);
      maxRetrievedNodes = Math.max(maxRetrievedNodes, (sub.nodes || []).length);
      maxRetrievedEdges = Math.max(maxRetrievedEdges, (sub.edges || []).length);
    }

    const wall = Number(process.hrtime.bigint() - t0) / 1e9;
    const cpu = (() => { const u = process.cpuUsage(c0); return (u.user + u.system) / 1e6; })();

    results.push({
      id: q.id, budgets: perBudget,
      retrieved_nodes: maxRetrievedNodes,
      retrieved_edges: maxRetrievedEdges,
      full_chars: maxFullChars,
      wall_s: +wall.toFixed(5), cpu_s: +cpu.toFixed(5),
    });
    process.stderr.write(`\r[koragraph:${qs.repo}] ${results.length}/${qs.questions.length}`);
  }
  process.stderr.write('\n');

  // The retrieval process cannot observe how the graph was built, so an `LLM_EXTRACTION` field
  // read from this process's env attested nothing — it recorded `on` (the || default) on runs the
  // README described as `off`, which is the kind of contradiction that discredits everything
  // around it. Replaced with the artifact that actually settles it: the project's own cost ledger,
  // grouped by provider, per CLAUDE.md's rule for any live run. Zero rows is the zero-API-cost
  // claim, evidenced rather than asserted.
  const { rows: costRows } = await pool.query(
    `SELECT provider, count(*)::int AS calls, COALESCE(sum(cost_usd), 0)::float AS usd
       FROM koragraph.cost_ledger WHERE project_id = $1 GROUP BY provider ORDER BY provider`,
    [opts.projectId]);

  const out = {
    system: 'koragraph',
    graph_engine: process.env.GRAPH_ENGINE || null,
    ingest_cost_ledger: costRows,
    ingest_external_api_calls: costRows.reduce((n, r) => n + r.calls, 0),
    // Resolved retrieval knobs. dotenv runs with override:true above, so koragraph_api/.env wins
    // over the process environment — without recording the resolved values, an arm cannot be
    // reproduced even internally, and two arms can differ for reasons no artifact shows.
    retrieval_config: {
      RETRIEVAL_TOP_K: process.env.RETRIEVAL_TOP_K || '8 (default)',
      RETRIEVAL_TOP_K_CEILING: process.env.RETRIEVAL_TOP_K_CEILING || '64 (default)',
      RETRIEVAL_ANCHOR_SEEDS: process.env.RETRIEVAL_ANCHOR_SEEDS || '4 (default)',
      RETRIEVAL_MIN_SCORE: process.env.RETRIEVAL_MIN_SCORE || '0.30 (default)',
      SUBGRAPH_MAX_NODES: process.env.SUBGRAPH_MAX_NODES || '25 (default)',
      SUBGRAPH_MAX_NODES_CEILING: process.env.SUBGRAPH_MAX_NODES_CEILING || '200 (default)',
      SUBGRAPH_AVG_TOKENS_PER_NODE: process.env.SUBGRAPH_AVG_TOKENS_PER_NODE || '72 (default)',
    },
    embedding: {
      profile_id: embedHealth.embedding_profile_id, mode: embedHealth.mode,
      capabilities: embedHealth.capabilities, backend: embedHealth.backend,
    },
    project_id: opts.projectId,
    node_count: counts.nodes, purpose_count: counts.purposes,
    language: qs.language, repo: qs.repo, pinned_sha: qs.pinned_sha,
    budgets,
    chars_per_token: CHARS_PER_TOKEN,
    invocation: 'graph-retriever.retrieveSubgraph(q, branchIds, [], {projectId, budget}) -> ' +
                'subgraph-builder.formatSubgraph(nodes, edges) — the shipped product path',
    results,
  };
  fs.mkdirSync(path.dirname(opts.out), { recursive: true });
  fs.writeFileSync(opts.out, JSON.stringify(out, null, 2));
  process.stderr.write(`[koragraph:${qs.repo}] ${results.length} questions x ${budgets.length} budgets -> ${opts.out}\n`);
  await pool.end();
}

main().catch(e => { console.error(e.message); process.exit(1); });
