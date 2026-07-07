// Synapse Portal Search Lab — wiring for the playground (live side-by-side compare) and the
// in-browser benchmark scoreboard. Works against ANY Synapse SearchIndex: curated tables
// (e.g. nf-tools) ship a golden set + hand-tuned boosts in data/<table>.json; any other
// index is loaded by synID, its schema discovered live, and its boosts auto-generated.
// Logic/data mirror the Python harness (synapse.js / strategies.js / score.js).

import { search, hitDict, hitId, indexName, indexColumns, listSearchIndexes } from "./synapse.js";
import { STRATEGIES, STRATEGY_ORDER, BOOSTED_KEYS } from "./strategies.js";
import { scoreCase, aggregate } from "./score.js";
import { generateFieldBoosts } from "./boostgen.js";
import { strategyLabel, METRIC_LABELS, toolTypeIcon } from "./labels.js";

// Shared SearchIndex collection project (children = every portal's index). See README.
const PROJECT = "syn74909065";
const TOOLS_INDEX = "syn75081636";  // nf-tools — the only index with per-type icons
const DISCOVERED = new Map();  // synID -> name, for indexes listed from the project

const METRIC_KEYS = ["mrr", "recall_at_k", "hit_at_1", "hit_at_k", "rt_ms_median", "rt_ms_p95"];
// Generic display-field preference (curated nf-tools columns first, then common fallbacks).
const NAME_KEYS = ["resourceName", "name", "title", "studyName", "label"];
const DESC_KEYS = ["description", "summary", "abstract", "overview"];
const TYPE_KEYS = ["resourceType", "type", "studyStatus", "category"];

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pick = (d, keys) => { for (const k of keys) if (d[k]) return d[k]; return null; };

let DATA = null;           // { index, index_name, k, id_field, fields, cases, precomputed, generated? }
let BOOSTS = [];           // [{ field, boost }] editable, seeded from DATA.fields
let LAST_BENCH = null;     // { strategies, per_case, k, live }

// ---------------------------------------------------------------- bootstrap
init().catch((e) => {
  $("main").insertAdjacentHTML("afterbegin", `<div class="errbox">Couldn’t start: ${esc(e.message)}</div>`);
});

async function init() {
  const manifest = await fetch("data/manifest.json").then((r) => (r.ok ? r.json() : { tables: [] })).catch(() => ({ tables: [] }));
  // one-time wiring (independent of which index is loaded)
  setupTabs();
  setupBoostCollapse();
  setupBoostEditor();
  setupColumns();
  setupSearch();
  setupBenchmarkControls();
  setupIndexSelector(manifest);
  if (manifest.generated_at) $("#genStamp").textContent = `Built ${manifest.generated_at}.`;

  // load the default (first curated) table, else prompt for a custom index
  const first = (manifest.tables || [])[0];
  if (first) await loadTable(first.table);
  else { $("#indexPick").value = "custom"; $("#customWrap").hidden = false; setStatus("pick or paste a SearchIndex to begin"); }
}

const parseBoost = (f) => { const [field, w] = f.split("^"); return { field, boost: w ? Number(w) : 1 }; };
const buildFields = () => BOOSTS.map(({ field, boost }) => (boost === 1 ? field : `${field}^${boost}`));
const setStatus = (html, cls = "") => { const el = $("#indexStatus"); el.className = `inst-status ${cls}`; el.innerHTML = html; };

// ---------------------------------------------------------------- index selector
function setupIndexSelector(manifest) {
  const sel = $("#indexPick");
  const curatedIds = new Set((manifest.tables || []).map((t) => t.index));
  const curatedOpts = (manifest.tables || []).map((t) =>
    `<option value="t:${esc(t.table)}">${esc(t.index_name || t.table)}</option>`).join("");
  // curated tables first (loadable immediately), then a fallback to paste any ID
  sel.innerHTML =
    (curatedOpts ? `<optgroup label="Curated — with golden set">${curatedOpts}</optgroup>` : "") +
    `<option value="custom">Other SearchIndex by ID…</option>`;

  sel.addEventListener("change", () => {
    const v = sel.value;
    if (v === "custom") { $("#customWrap").hidden = false; $("#customId").focus(); return; }
    $("#customWrap").hidden = true;
    if (v.startsWith("t:")) loadTable(v.slice(2));
    else if (v.startsWith("i:")) loadCustomIndex(v.slice(2), DISCOVERED.get(v.slice(2)));
  });
  const go = () => loadCustomIndex($("#customId").value.trim());
  $("#loadCustom").addEventListener("click", go);
  $("#customId").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });

  // async: list every SearchIndex in the project and add the non-curated ones, so users
  // can pick any portal's index without pasting an ID. Optional — paste-by-ID still works.
  listSearchIndexes(PROJECT).then((rows) => {
    const others = rows.filter((r) => !curatedIds.has(r.id));
    if (!others.length) return;
    others.forEach((r) => DISCOVERED.set(r.id, r.name));
    const og = document.createElement("optgroup");
    og.label = `All SearchIndexes in project (${rows.length})`;
    og.innerHTML = others.map((r) => `<option value="i:${esc(r.id)}">${esc(r.name)}</option>`).join("");
    sel.insertBefore(og, sel.querySelector('option[value="custom"]'));
  }).catch(() => {});
}

async function loadTable(table) {
  setStatus("loading…");
  try {
    const data = await fetch(`data/${table}.json`).then((r) => { if (!r.ok) throw new Error(`${r.status}`); return r.json(); });
    $("#indexPick").value = `t:${table}`;
    applyData(data);
  } catch (e) { setStatus(`couldn’t load ${esc(table)} (${esc(e.message)})`, "err"); }
}

async function loadCustomIndex(synId, knownName = null) {
  if (!/^syn\d+$/i.test(synId)) { setStatus("enter a valid synID, e.g. syn75081633", "err"); return; }
  $("#loadCustom").disabled = true;
  setStatus(`discovering ${esc(synId)}…`);
  try {
    const [name, columns] = await Promise.all([
      knownName ? Promise.resolve(knownName) : indexName(synId),
      indexColumns(synId),
    ]);
    if (!columns.length) throw new Error("no columns found — is this a public SearchIndex?");
    applyData({
      index: synId, index_name: name, k: 10, id_field: "rowId",
      fields: generateFieldBoosts(columns), cases: [], precomputed: null, columns, generated: true,
    });
    // reflect the load in the picker: select its option if listed, else fall back to "custom"
    const sel = $("#indexPick");
    const opt = sel.querySelector(`option[value="i:${synId}"]`);
    sel.value = opt ? `i:${synId}` : "custom";
  } catch (e) {
    setStatus(`couldn’t load ${esc(synId)}: ${esc(e.message)}`, "err");
  } finally { $("#loadCustom").disabled = false; }
}

// Real example queries from the index's own golden set beat generic placeholder text —
// but not every index has one (auto-boosted indexes ship with cases: []).
function updateSearchPlaceholder(data) {
  const examples = (data.cases || []).slice(0, 3).map((c) => c.query).filter(Boolean);
  $("#q").placeholder = examples.length
    ? `Search ${data.index_name} — e.g. ${examples.map((e) => `“${e}”`).join(", ")}…`
    : `Search ${data.index_name}…`;
}

// (re)render everything that depends on the loaded index
function applyData(data) {
  DATA = data;
  BOOSTS = (data.fields || []).map(parseBoost);
  const golden = data.cases.length ? `${data.cases.length} golden cases` : "no golden set";
  // when we discovered all columns live, show boostable as a subset of the total so it's
  // clear why the two numbers differ (non-text column types are excluded — see the
  // "Which column types can be boosted?" note in the sidebar).
  const fields = data.columns
    ? `${data.columns.length} columns (${BOOSTS.length} boostable)`
    : `${BOOSTS.length} boostable fields`;
  const gen = data.generated ? ` · <span class="ok">auto-boosts</span>` : "";
  setStatus(`<span class="ok">${esc(data.index_name)}</span> · ${esc(data.index)} · ${fields} · ${golden}${gen}`);
  updateSearchPlaceholder(data);
  $("#sizeInput").value = data.k || 10;
  renderBoostEditor();
  // clear stale playground results from the previous index
  $$(".col .results").forEach((ul) => { ul.innerHTML = ""; });
  $$(".col .col-meta").forEach((m) => m.remove());
  $("#compareSummary").hidden = true;
  lastQuery = "";
  setBenchMode();
}

// ---------------------------------------------------------------- tabs
function setupTabs() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => { t.classList.toggle("is-active", t === tab); t.setAttribute("aria-selected", t === tab); });
    $$(".panel-tab").forEach((p) => { const on = p.id === tab.dataset.tab; p.classList.toggle("is-active", on); p.hidden = !on; });
  }));
}

// ---------------------------------------------------------------- boosts collapse-to-side
function setBoostsCollapsed(on) {
  $(".layout").classList.toggle("boosts-collapsed", on);
  $("#boostToggle").setAttribute("aria-expanded", String(!on));
}
function setupBoostCollapse() {
  $("#boostToggle").addEventListener("click", () => setBoostsCollapsed(true));
  $("#boostExpand").addEventListener("click", () => setBoostsCollapsed(false));
}

// ---------------------------------------------------------------- boost editor
function setupBoostEditor() {
  const ed = $("#boostEditor");
  ed.addEventListener("input", (e) => {
    const i = e.target.dataset.i;
    if (i != null) BOOSTS[i].boost = Math.max(0, Number(e.target.value) || 0);
  });
  $("#resetBoosts").addEventListener("click", () => { BOOSTS = BOOSTS.map(({ field }) => ({ field, boost: 1 })); renderBoostEditor(); });
}
function renderBoostEditor() {
  $("#boostEditor").innerHTML = BOOSTS.map((b, i) =>
    `<label class="boost-field"><code>${esc(b.field)}</code>
       <input type="number" min="0" max="20" step="1" value="${b.boost}" data-i="${i}"></label>`).join("");
}

// ---------------------------------------------------------------- playground
function setupColumns() {
  const opts = STRATEGY_ORDER.map((k) =>
    `<option value="${k}"${BOOSTED_KEYS.has(k) ? ' title="Uses Custom Field Boosts"' : ""}>${esc(strategyLabel(k).name)}</option>`
  ).join("");
  const defaults = { A: "frontend_default", B: "multi_match_cross" };
  $$(".col").forEach((col) => {
    const which = col.dataset.col;
    col.innerHTML =
      `<div class="col-head">
         <div class="col-pickrow">
           <select class="col-pick" aria-label="Recipe for column ${which}">${opts}</select>
           <span class="info">
             <span class="info-trigger" tabindex="0" role="button" aria-label="About this recipe">i</span>
             <span class="col-tip" role="tooltip"></span>
           </span>
         </div>
         <span class="boost-ref"></span>
       </div>
       <ul class="results"></ul>`;
    const sel = $(".col-pick", col);
    sel.value = defaults[which];
    const refreshBlurb = () => {
      const l = strategyLabel(sel.value);
      $(".col-tip", col).innerHTML = `${esc(l.blurb)} <span class="best-for">${esc(l.bestFor)}</span>`;
      const boosted = BOOSTED_KEYS.has(sel.value);
      const ref = $(".boost-ref", col);
      ref.textContent = boosted ? "→ with Custom Field Boosts" : "no boosts";
      ref.classList.toggle("is-boosted", boosted);
      refreshBoostLink();
    };
    refreshBlurb();
    sel.addEventListener("change", () => { refreshBlurb(); if (lastQuery) runPlayground(); });
  });
}

// Highlights the "Custom Field Boosts" sidebar panel whenever a currently-selected recipe
// (in either column) actually reads it, so the link between the two is obvious — and keeps
// the panel's open/collapsed state in sync: pops it open once a boosted recipe is picked,
// tucks it away again once neither column needs it.
function refreshBoostLink() {
  const linked = $$(".col-pick").some((sel) => BOOSTED_KEYS.has(sel.value));
  $(".boost-card")?.classList.toggle("is-linked", linked);
  $(".boost-rail")?.classList.toggle("is-linked", linked);
  const collapsed = $(".layout").classList.contains("boosts-collapsed");
  if (linked && collapsed) setBoostsCollapsed(false);
  else if (!linked && !collapsed) setBoostsCollapsed(true);
}

let lastQuery = "";
function setupSearch() {
  $("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); runPlayground(); });
}

async function runPlayground() {
  const q = $("#q").value.trim();
  lastQuery = q;
  if (!q || !DATA) return;
  const size = Math.max(1, Number($("#sizeInput").value) || 10);
  const fields = buildFields();
  const cols = $$(".col");
  cols.forEach((c) => { $(".results", c).innerHTML = `<li class="loading">searching…</li>`; });
  $("#searchForm .go").disabled = true;

  const runs = await Promise.all(cols.map(async (col) => {
    const key = $(".col-pick", col).value;
    try {
      const res = await search(DATA.index, STRATEGIES[key](q, size, fields), { responseParts: ["HITS", "TOTAL_HITS"] });
      return { col, hits: res.hits || [], total: res.totalHits };
    } catch (err) { return { col, error: err.message }; }
  }));

  const rankMaps = runs.map((r) => {
    const m = new Map();
    (r.hits || []).forEach((h, i) => m.set(hitId(h, DATA.id_field), i + 1));
    return m;
  });
  runs.forEach((r, i) => renderColumn(r, rankMaps[1 - i]));
  renderCompareSummary(runs, rankMaps);
  $("#searchForm .go").disabled = false;
}

// How much the two columns' result sets overlap (shown above the comparison).
function renderCompareSummary(runs, [A, B]) {
  const box = $("#compareSummary");
  // only meaningful when both sides returned results
  if (runs.some((r) => r.error) || !A.size || !B.size) { box.hidden = true; return; }
  const labels = runs.map((r) => strategyLabel($(".col-pick", r.col).value).name);
  const shared = [...A.keys()].filter((id) => B.has(id));
  const sameSpot = shared.filter((id) => A.get(id) === B.get(id)).length;
  const onlyA = A.size - shared.length;
  const onlyB = B.size - shared.length;
  const n = Math.max(A.size, B.size);
  const stat = (v, label) => `<span class="cs-stat"><b>${v}</b> ${label}</span>`;
  box.hidden = false;
  box.innerHTML =
    `<span class="cs-lead">${shared.length} of ${n} results in common</span>` +
    stat(sameSpot, "in the same position") +
    stat(onlyA, `only in <em>${esc(labels[0])}</em>`) +
    stat(onlyB, `only in <em>${esc(labels[1])}</em>`);
}

function renderColumn({ col, hits, total, error }, otherRanks) {
  const ul = $(".results", col);
  if (error) { ul.innerHTML = `<li class="errbox">Query failed: ${esc(error)}</li>`; return; }
  if (!hits.length) { ul.innerHTML = `<li class="empty">No results.</li>`; return; }
  let meta = $(".col-meta", col);
  if (!meta) { meta = document.createElement("p"); meta.className = "col-meta"; $(".col-head", col).appendChild(meta); }
  meta.textContent = `${hits.length} shown · ${total ?? "?"} total matches`;

  ul.innerHTML = hits.map((h, i) => {
    const d = hitDict(h);
    const rank = i + 1;
    const id = hitId(h, DATA.id_field);
    const delta = deltaBadge(rank, otherRanks.get(id));
    const name = pick(d, NAME_KEYS) || `(row ${esc(h.rowId ?? "?")})`;
    const type = pick(d, TYPE_KEYS);
    const desc = pick(d, DESC_KEYS);
    const isTools = DATA.index === TOOLS_INDEX;
    const typeChip = type ? `<span class="chip type-chip">${isTools ? toolTypeIcon(type) : ""}${esc(type)}</span>` : "";
    const chips = [typeChip, d.rrid ? `<span class="chip">${esc(d.rrid)}</span>` : ""].join("");
    return `<li class="hit" style="animation-delay:${i * 28}ms">
      <span class="rank">${rank}</span>${delta}
      <div class="name">${esc(name)}</div>
      <div class="sub">${chips}<span class="score">score ${fmtScore(h.score)}</span></div>
      ${desc ? `<div class="desc">${esc(desc)}</div>` : ""}
      <div class="rid">${esc(id ?? "")}</div>
    </li>`;
  }).join("");
}

function deltaBadge(rank, oRank) {
  if (oRank == null) return `<span class="delta new" title="only on this side">new</span>`;
  const diff = oRank - rank;
  if (diff === 0) return `<span class="delta same" title="same position">=</span>`;
  return `<span class="delta ${diff > 0 ? "up" : "down"}" title="vs other side">${diff > 0 ? "▲" : "▼"}${Math.abs(diff)}</span>`;
}
const fmtScore = (s) => (typeof s === "number" ? s.toFixed(2) : "—");

// ---------------------------------------------------------------- benchmark
function setupBenchmarkControls() {
  const picker = $("#stratPicker");
  picker.insertAdjacentHTML("beforeend", `<div class="strat-grid">${STRATEGY_ORDER.map((k) =>
    `<label class="strat-chip on"><input type="checkbox" value="${k}" checked>${esc(strategyLabel(k).name)}</label>`).join("")}</div>`);
  picker.addEventListener("change", (e) => { e.target.closest(".strat-chip")?.classList.toggle("on", e.target.checked); updateEstimate(); });
  $("#runBench").addEventListener("click", runBenchmark);
}

// enable/disable benchmarking for the loaded index (needs a golden set)
function setBenchMode() {
  const hasGolden = DATA.cases.length > 0;
  $(".bench-controls").hidden = !hasGolden;
  $("#benchProgress").hidden = true;
  $("#benchResult").innerHTML = "";
  const notice = $("#benchNotice");
  notice.hidden = hasGolden;
  if (!hasGolden) {
    notice.innerHTML =
      `<svg class="notice-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
            stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M5.8 5.8l12.4 12.4"/></svg>
       <span>No golden set for <strong>${esc(DATA.index_name)}</strong> — benchmarking needs curated
       queries with known-correct results. Try the <strong>Search playground</strong> instead.</span>`;
    return;
  }
  updateEstimate();
  if (DATA.precomputed) {
    LAST_BENCH = { ...DATA.precomputed, k: DATA.precomputed.k || DATA.k, live: false };
    renderBench(LAST_BENCH, `Showing the precomputed baseline (${esc(DATA.precomputed.label || "baseline")}). Run live to recompute with your boost settings.`);
  }
}

const selectedStrategies = () => $$('#stratPicker input:checked').map((i) => i.value);
function updateEstimate() {
  const n = selectedStrategies().length * (DATA?.cases.length || 0);
  $("#benchEst").textContent = n ? `${n} queries (~${Math.ceil(n / 6)}s)` : "select at least one recipe";
}

async function runBenchmark() {
  const keys = selectedStrategies();
  if (!keys.length || !DATA.cases.length) return;
  const k = DATA.k || 10;
  const size = Math.max(k, 10);
  const fields = buildFields();
  const tasks = [];
  keys.forEach((sk) => DATA.cases.forEach((c) => tasks.push({ sk, c })));

  const acc = Object.fromEntries(keys.map((sk) => [sk, { caseScores: [], rts: [], perCase: {} }]));
  $("#benchProgress").hidden = false;
  $("#runBench").disabled = true;
  let done = 0;
  const tick = () => {
    const pct = Math.round((done / tasks.length) * 100);
    $("#progressFill").style.width = `${pct}%`;
    $("#progressText").textContent = `${done} / ${tasks.length} queries · ${pct}%`;
  };
  tick();

  let cursor = 0;
  const worker = async () => {
    while (cursor < tasks.length) {
      const { sk, c } = tasks[cursor++];
      const t0 = performance.now();
      let ranked = [];
      try {
        const res = await search(DATA.index, STRATEGIES[sk](c.query, size, fields), { responseParts: ["HITS", "TOTAL_HITS"] });
        ranked = (res.hits || []).map((h) => hitId(h, DATA.id_field));
      } catch { /* terminal failure → counts as a miss */ }
      const rt = performance.now() - t0;
      const sc = scoreCase(ranked, c.relevant, k);
      sc.rt_ms = rt;
      acc[sk].caseScores.push(sc); acc[sk].rts.push(rt); acc[sk].perCase[c.id] = sc;
      done++; tick();
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));

  const strategies = {}, per_case = {};
  keys.forEach((sk) => { strategies[sk] = aggregate(acc[sk].caseScores, acc[sk].rts); per_case[sk] = acc[sk].perCase; });
  LAST_BENCH = { strategies, per_case, k, live: true };
  $("#benchProgress").hidden = true;
  $("#runBench").disabled = false;
  renderBench(LAST_BENCH, `Live run · ${tasks.length} queries against ${esc(DATA.index_name)} · k=${k}.`);
}

// `reverse` flips the metric's natural "best first" direction — toggled by clicking the
// already-sorted column again, so a second click does something instead of re-sorting
// into the exact same order.
let sortState = { key: "mrr", reverse: false };
function renderBench(bench, note) {
  const k = bench.k;
  const rows = Object.entries(bench.strategies);
  const dir = (key) => (METRIC_LABELS[key].dir === "up" ? -1 : 1) * (sortState.reverse ? -1 : 1);
  rows.sort((a, b) => (a[1][sortState.key] - b[1][sortState.key]) * dir(sortState.key));

  const best = {};
  METRIC_KEYS.forEach((key) => {
    const vals = rows.map((r) => r[1][key]).filter((v) => typeof v === "number");
    best[key] = METRIC_LABELS[key].dir === "up" ? Math.max(...vals) : Math.min(...vals);
  });

  const head = METRIC_KEYS.map((key) => {
    const m = METRIC_LABELS[key];
    const cls = sortState.key === key ? `sorted${sortState.reverse ? " reverse" : ""}` : "";
    return `<th data-key="${key}" title="${esc(m.help)}" class="${cls}">${esc(m.name.replace("@k", `@${k}`))}</th>`;
  }).join("");

  const body = rows.map(([name, a]) => {
    const cells = METRIC_KEYS.map((key) => {
      const v = a[key] ?? 0;
      const isBest = typeof v === "number" && Math.abs(v - best[key]) < 1e-9;
      const txt = key.startsWith("rt_ms") ? Math.round(v) : v.toFixed(3);
      return `<td class="metric ${isBest ? "best" : ""}">${txt}</td>`;
    }).join("");
    return `<tr><td class="stratname">${esc(strategyLabel(name).name)}<small>${esc(name)}</small></td>${cells}</tr>`;
  }).join("");

  $("#benchResult").innerHTML =
    `<h2>Scoreboard</h2>
     <table class="scoreboard">
       <thead><tr><th>Recipe</th>${head}</tr></thead>
       <tbody>${body}</tbody>
     </table>
     <p class="score-note">${note} ★ marks the best recipe per column. Click a column header to re-sort.</p>
     ${bench.per_case ? casesSection(bench) : ""}`;

  $$(".scoreboard thead th[data-key]").forEach((th) => th.addEventListener("click", () => {
    sortState.reverse = sortState.key === th.dataset.key ? !sortState.reverse : false;
    sortState.key = th.dataset.key;
    renderBench(bench, note);
  }));
}

function casesSection(bench) {
  const keys = Object.keys(bench.strategies);
  const head = keys.map((k) => `<th title="${esc(k)}">${esc(strategyLabel(k).name)}</th>`).join("");
  const rows = DATA.cases.map((c) => {
    const cells = keys.map((sk) => {
      const r = bench.per_case[sk]?.[c.id]?.first_rel_rank;
      return `<td class="${rankClass(r)}">${r ? `<span class="rankcell ${rankClass(r)}">${r}</span>` : "—"}</td>`;
    }).join("");
    return `<tr><td class="q">${esc(c.query)}<br><small>${esc(c.type || "")}</small></td>${cells}</tr>`;
  }).join("");
  return `<details class="cases-toggle"><summary>Per-case detail — rank of the first correct result (lower is better)</summary>
    <div style="overflow:auto"><table class="case-table">
      <thead><tr><th>Query</th>${head}</tr></thead><tbody>${rows}</tbody>
    </table></div></details>`;
}
const rankClass = (r) => (!r ? "miss" : r === 1 ? "r1" : r <= 3 ? "good" : r <= 10 ? "mid" : "miss");
