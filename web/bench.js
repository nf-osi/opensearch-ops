// Benchmark results — the dashboard that opens the site. Reports the runs committed in
// benchmark/<table>/results/ (assembled into data/*.json by build_site.py); it never
// queries Synapse. Live querying and live scoring live in the search lab (app.js).
//
// Two levels, in reading order:
//   Portfolio  — every nf- SearchIndex: which ones have a golden set, and for those with
//                a run, the platform default vs the best recipe tested.
//   Index      — one index in depth: recipe leaderboard, where correct answers land,
//                quality vs latency, case-type split, per-case ranks, the searches that
//                fail, how the golden set was built, and the field boosts in play.

import { hbar, dumbbell, scatter, rankstack, heatmap, legend, tableTwin, defsTwin, rankBucket,
         ROLE, RANK_BANDS, fmtNum, fmtMs } from "./charts.js";
import { strategyLabel, METRIC_LABELS, CASE_TYPE_LABELS } from "./labels.js";
import { anchorLink, setRoute, scrollToSection } from "./route.js";

const $ = (sel, root = document) => root.querySelector(sel);
const METRIC_KEYS = ["mrr", "recall_at_k", "hit_at_1", "hit_at_k", "rt_ms_median", "rt_ms_p95"];
const RANK_BY = ["mrr", "recall_at_k", "hit_at_1", "rt_ms_median"];

let MANIFEST = null;
const CACHE = new Map();                 // table -> data/<table>.json
const state = { table: null, runLabel: null, rankBy: "mrr", filters: { type: "all", source: "all", failing: false } };

// ---------------------------------------------------------------- small DOM helpers
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;                 // all label text is untrusted
  return n;
}
const isMs = (key) => key.startsWith("rt_ms");
const fmtMetric = (key, v) => (isMs(key) ? fmtMs(v) : fmtNum(v));
const metricName = (key, k) => METRIC_LABELS[key].name.replace("@k", `@${k}`);
const pct = (n, d) => (d ? `${Math.round((n / d) * 100)}%` : "—");
const day = (iso) => (iso ? String(iso).slice(0, 10) : "—");

/** A section shell: eyebrow + heading + optional lede, then a body to fill.
 *  `slug` makes the section addressable as #/results/<index>/<slug>. */
function block(host, { eyebrow, title, lede, slug }) {
  const sec = el("section", "block");
  if (slug) sec.id = slug;
  const head = el("header", "block-head");
  if (eyebrow) head.appendChild(el("p", "block-eyebrow", eyebrow));
  const h = el("h2", null, title);
  if (slug) h.appendChild(anchorLink(slug, { table: state.table }));
  head.appendChild(h);
  if (lede) head.appendChild(el("p", "block-lede", lede));
  const body = el("div", "block-body");
  sec.append(head, body);
  host.appendChild(sec);
  return body;
}

/** A sub-section heading inside a block — addressable the same way. */
function subHead(text, slug) {
  const h = el("h3", "sub-head", text);
  if (slug) h.appendChild(anchorLink(slug, { table: state.table }));
  return h;
}

/** A figure card: title, the chart mount, then legend + table twin under it. */
function figure(host, { title, note }) {
  const fig = el("figure", "fig");
  const cap = el("figcaption", "fig-cap");
  cap.appendChild(el("h3", null, title));
  if (note) cap.appendChild(el("p", "fig-note", note));
  const plot = el("div", "fig-plot");
  fig.append(cap, plot);
  host.appendChild(fig);
  return { fig, plot };
}

/** Segmented control — standard form controls styled to match the chart chrome. */
function segmented({ label, options, value, onChange }) {
  const wrap = el("div", "seg");
  if (label) wrap.appendChild(el("span", "seg-label", label));
  const group = el("div", "seg-group");
  group.setAttribute("role", "radiogroup");
  for (const opt of options) {
    const b = el("button", `seg-btn${opt.value === value ? " is-on" : ""}`, opt.label);
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(opt.value === value));
    b.addEventListener("click", () => onChange(opt.value));
    group.appendChild(b);
  }
  wrap.appendChild(group);
  return wrap;
}

// ---------------------------------------------------------------- run helpers
/* Three reference points. The platform default is the absolute control and is always
   `frontend_default`, on every index. `production_current` is the contextual control —
   what this portal page actually sends today — and exists only where the portal ships a
   SearchQueryConfig. Best wins the tie when a reference point is also the winner, since
   "this already won" is the more surprising fact; the glossary states both. */
const PLATFORM_DEFAULT_KEY = "frontend_default";
const PRODUCTION_KEY = "production_current";

const roleOf = (key, run) => (
  key === run.best_key ? ROLE.BEST
    : key === PRODUCTION_KEY ? ROLE.PRODUCTION
    : key === PLATFORM_DEFAULT_KEY ? ROLE.BASELINE
    : ROLE.OTHER);

/** Normalise one committed run into the shape the views want, resolving which recipe is
 *  the control and which won on MRR.
 *
 *  The control is per-table: an index whose portal page sets a SearchQueryConfig is
 *  measured against `production_current` (what that page really sends), one that does not
 *  against the platform default. run.py records the choice on the run; runs written before
 *  that fall back to the manifest's default. */
function readRun(data, run) {
  const strategies = run.strategies || {};
  const keys = Object.keys(strategies);
  const ranked = [...keys].sort((a, b) => (strategies[b].mrr || 0) - (strategies[a].mrr || 0));
  const control = run.control || MANIFEST.default_baseline_key;
  return {
    label: run.label, run_at: run.run_at, k: run.k || data.k,
    strategies, keys, per_case: run.per_case || {},
    // rows pinned to another run (site.yaml `constant:`) — {strategy: {from, run_at}}
    constants: run.constants || {},
    baseline_key: keys.includes(control) ? control : null,
    best_key: ranked[0] || null,
  };
}

/** Mean of a per-case field over the cases a filter admits. */
function meanOver(perCase, ids, field) {
  const vals = ids.map((id) => perCase[id]?.[field]).filter((v) => typeof v === "number");
  return vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : null;
}

function bucketsFor(run, key, ids) {
  const out = { top: 0, near: 0, deep: 0, miss: 0 };
  const perCase = run.per_case[key] || {};
  for (const id of ids) {
    if (!(id in perCase)) continue;
    out[rankBucket(perCase[id].first_rel_rank, run.k)] += 1;
  }
  return out;
}

/** Case ids the run actually covered, in golden-set order (goldens grow between runs). */
function ranCaseIds(data, run) {
  const seen = new Set(Object.values(run.per_case).flatMap((pc) => Object.keys(pc)));
  return data.cases.map((c) => c.id).filter((id) => seen.has(id));
}

// ---------------------------------------------------------------- boot
export async function initResults(route = {}) {
  MANIFEST = await fetch("data/manifest.json").then((r) => r.json());
  renderHero();
  renderPortfolio();
  await showRoute(route, { scroll: true });
  $("#builtStamp").textContent = MANIFEST.generated_at ? `Built ${MANIFEST.generated_at}` : "";
  return MANIFEST;
}

/** Apply the index + section parts of a URL. A link naming an index we don't have
 *  (renamed, or its golden set removed) falls back to the default rather than 404ing
 *  the page — the link still lands on the dashboard. */
export async function showRoute({ table, section } = {}, { scroll = false } = {}) {
  const known = MANIFEST.tables.some((t) => t.table === table);
  await selectTable(known ? table : defaultTable(), { silent: true });
  if (scroll && section) scrollToSection(section, { smooth: false });
  return state.table;
}

/** The index with the most to show — cases scored x recipes compared — rather than
 *  whichever table sorts first alphabetically. */
function defaultTable() {
  const depth = (t) => (t.latest ? (t.latest.n_cases_run || 0) * Object.keys(t.latest.strategies).length : -1);
  return [...MANIFEST.tables].sort((a, b) => depth(b) - depth(a))[0]?.table;
}

// ---------------------------------------------------------------- hero
// The finding, stated as a sentence with the number set large inside it — one hero figure
// per view. Pools every scored case in every index that has a committed run.
function renderHero() {
  const scored = MANIFEST.tables.filter((t) => t.latest?.buckets);
  const pool = (which) => scored.reduce((acc, t) => {
    const b = t.latest.buckets[t.latest[which]];
    if (b) for (const band of ["top", "near", "deep", "miss"]) acc[band] += b[band];
    return acc;
  }, { top: 0, near: 0, deep: 0, miss: 0 });
  const today = pool("baseline_key"), best = pool("best_key");
  const nToday = Object.values(today).reduce((s, v) => s + v, 0);
  const nBest = Object.values(best).reduce((s, v) => s + v, 0);
  const host = $("#hero");
  host.replaceChildren();
  host.appendChild(el("p", "hero-eyebrow", "Experimental gains"));

  // Relative lift, not the control's own score — the finding is what tuning buys, and a
  // control result read as a headline invites "58% is bad" rather than "+17% is available".
  const topToday = nToday ? today.top / nToday : 0;
  const topBest = nBest ? best.top / nBest : 0;
  const relLift = topToday ? Math.round(((topBest / topToday) - 1) * 100) : 0;
  const ptLift = Math.round(topBest * 100) - Math.round(topToday * 100);

  const thesis = el("h2", "hero-thesis");
  if (nToday && relLift > 0) {
    thesis.appendChild(el("span", "hero-fig", `${relLift}%`));
    thesis.append(document.createTextNode(" more NF searches get the right answer first than on the platform default."));
  } else if (nToday) {
    thesis.append(document.createTextNode("No recipe tested beats the platform default, which answers "));
    thesis.appendChild(el("span", "hero-fig", pct(today.top, nToday)));
    thesis.append(document.createTextNode(" of curated NF searches first-try."));
  } else {
    thesis.textContent = "The ground truth is curated. Nothing is scored yet.";
  }
  host.appendChild(thesis);
  host.appendChild(el("p", "hero-sub", nToday
    ? `${pct(today.top, nToday)} → ${pct(best.top, nBest)} of ${nToday} curated searches, a ${ptLift}-point gain across ${scored.length} of ${MANIFEST.coverage.n_indexes} nf- indexes. Experiments cover both levers, the boosted query strategy as well as the index's custom search configuration.`
    : `${MANIFEST.coverage.n_cases} golden searches are curated across ${MANIFEST.coverage.n_benchmarked} indexes. Run benchmark/run.py and commit the results to fill this in.`));

  if (!nToday) return;
  const tile = (role, label, value, delta, sub, tone) => {
    const t = el("div", `stat stat-${role}`);
    t.appendChild(el("p", "stat-label", label));
    t.appendChild(el("p", "stat-value", value));
    if (delta) t.appendChild(el("p", `stat-delta${tone ? ` is-${tone}` : ""}`, delta));
    t.appendChild(el("p", "stat-sub", sub));
    return t;
  };
  // Round-trip is measured client-side per case, so there is no pooled median to read off
  // the aggregates — take the per-index medians weighted by the cases each run covered.
  const rt = (which) => {
    let acc = 0, n = 0;
    for (const t of scored) {
      const v = t.latest.strategies[t.latest[which]]?.rt_ms_median, w = t.latest.n_cases_run || 0;
      if (typeof v === "number" && w) { acc += v * w; n += w; }
    }
    return n ? acc / n : null;
  };
  const rtToday = rt("baseline_key"), rtBest = rt("best_key");
  const figs = el("div", "hero-figs");
  // The headline already carries the pooled gain, so the first tile answers a different
  // question: is the gain broad, or one index dragging the pool? Counted per index rather
  // than pooled, because an index already at its best is the useful negative result.
  const withRun = scored.filter((t) => t.latest.baseline_key && t.latest.best_key);
  const headroom = withRun.filter((t) =>
    (t.latest.strategies[t.latest.best_key]?.mrr ?? 0) >
    (t.latest.strategies[t.latest.baseline_key]?.mrr ?? 0) + 1e-9);
  const atBest = withRun.filter((t) => !headroom.includes(t)).map((t) => t.index_name);
  figs.append(
    tile("best", "Indexes with gains", `${headroom.length} of ${withRun.length}`,
      headroom.length ? `best gain +${fmtNum(Math.max(...headroom.map((t) =>
        t.latest.strategies[t.latest.best_key].mrr - t.latest.strategies[t.latest.baseline_key].mrr)))} MRR` : null,
      atBest.length
        ? `Scored indexes where some tested recipe can beat the deafult. ${atBest.join(", ")} ${atBest.length === 1 ? "is" : "are"} already at the best recipe tested. Gains are not uniform; not every index can benefit from customization.`
        : `Every scored index has a tested recipe that beats the default.`),
    tile("baseline", "Searches that miss entirely", String(today.miss),
      `${pct(today.miss, nToday)} of ${nToday} curated searches`,
      `No correct result anywhere in the top ${scored[0].latest.k} on the platform default. The best recipe tested misses ${best.miss} (${pct(best.miss, nBest)}).`,
      "quiet"),
  );
  if (rtBest != null && rtToday != null) {
    const faster = rtToday - rtBest;
    // as a share of the default, to read the same way as the +N points above
    const shift = Math.round((Math.abs(faster) / rtToday) * 100);
    figs.appendChild(tile("best", "Best recipe latency", fmtMs(rtBest),
      shift < 1 ? null : `${faster > 0 ? "−" : "+"}${shift}% vs the platform default`,
      `Median search-to-results wait, pooled across ${scored.length} indexes; the platform default measures ${fmtMs(rtToday)}.`,
      faster > 0 ? null : "worse"));
  }
  host.appendChild(figs);
}

// ---------------------------------------------------------------- portfolio
function renderPortfolio() {
  const host = $("#portfolio");
  host.replaceChildren();
  const body = block(host, {
    slug: "portfolio",
    eyebrow: "Portfolio",
    title: "Every NF search index",
    lede: `${MANIFEST.coverage.n_benchmarked} of ${MANIFEST.coverage.n_indexes} nf- indexes have a golden set; ${MANIFEST.coverage.n_configured} have a search config committed in config/. Pick an index to see its results in detail.`,
  });

  // index cards — the selector for the detail section below
  const cards = el("div", "index-cards");
  for (const row of MANIFEST.registry) {
    const t = row.table ? MANIFEST.tables.find((x) => x.table === row.table) : null;
    const card = el(t ? "button" : "div", `index-card${t ? "" : " is-empty"}`);
    if (t) {
      card.type = "button";
      card.dataset.table = t.table;
      card.addEventListener("click", () => selectTable(t.table));
    }
    card.appendChild(el("p", "ic-name", row.index_name));
    const meta = el("p", "ic-meta");
    meta.appendChild(el("span", "ic-id", row.index));
    if (row.configured) meta.appendChild(el("span", "ic-flag", "config"));
    card.appendChild(meta);
    if (t?.latest) {
      const b = t.latest.buckets[t.latest.baseline_key];
      const n = b ? Object.values(b).reduce((s, v) => s + v, 0) : 0;
      card.appendChild(el("p", "ic-stat", n ? `${pct(b.top, n)} answered first` : "run committed"));
      if (b) card.appendChild(rankStrip(b, n));
      card.appendChild(el("p", "ic-sub", `${t.n_cases} cases · run ${day(t.latest.run_at)}`));
    } else if (t) {
      card.appendChild(el("p", "ic-stat is-quiet", "not scored yet"));
      card.appendChild(el("p", "ic-sub", `${t.n_cases} golden cases ready`));
    } else {
      card.appendChild(el("p", "ic-stat is-quiet", "no golden set"));
    }
    cards.appendChild(card);
  }
  body.appendChild(cards);

  // The cards carry rank strips, which are the same four bands the per-search-type figure
  // plots — so they get the same legend rather than leaving the colours unexplained.
  // k is per-index in principle; only state a number when every scored index agrees.
  const ks = [...new Set(MANIFEST.tables.map((t) => t.latest?.k).filter(Boolean))];
  const kLabel = ks.length === 1 ? String(ks[0]) : "k";
  const anyStrip = MANIFEST.tables.some((t) => t.latest?.buckets?.[t.latest.baseline_key]);
  if (anyStrip) {
    body.appendChild(legend(
      RANK_BANDS.map((b) => ({ fill: b.fill, label: b.label.replace("{k}", kLabel) })),
      "Each bar above: where the first correct result landed, across that index's golden cases.",
    ));
  }

  // baseline -> best, per index that has a run
  const scored = MANIFEST.tables.filter((t) => t.latest?.baseline_key && t.latest?.best_key);
  if (scored.length) {
    const { plot } = figure(body, {
      title: "Where each index stands",
      note: "Mean reciprocal rank of the first correct result — how near the top the right answer lands. Left dot is the platform default; right dot is the best recipe tested.",
    });
    const rows = scored.map((t) => ({
      label: t.index_name,
      from: t.latest.strategies[t.latest.baseline_key]?.mrr ?? 0,
      to: t.latest.strategies[t.latest.best_key]?.mrr ?? 0,
    }));
    dumbbell(plot, { rows, max: 1, fromLabel: "today", toLabel: "best tested" });
    plot.after(legend([
      { fill: "var(--series-2)", label: "Platform default" },
      { fill: "var(--series-1)", label: "Best recipe tested" },
    ]));
    plot.parentElement.appendChild(tableTwin({
      summary: "Table view — MRR by index",
      head: ["Index", "Today (MRR)", "Best tested (MRR)", "Best recipe", "Run"],
      align: [null, "num", "num", null, null],
      rows: scored.map((t) => [t.index_name, fmtNum(t.latest.strategies[t.latest.baseline_key]?.mrr),
        fmtNum(t.latest.strategies[t.latest.best_key]?.mrr),
        strategyLabel(t.latest.best_key).name, day(t.latest.run_at)]),
    }));
  }
}

/** The recurring motif: a 4-band strip of where this index's answers land. */
function rankStrip(buckets, total) {
  const strip = el("div", "rank-strip");
  strip.setAttribute("aria-hidden", "true");                  // the numbers are stated in text
  for (const band of RANK_BANDS) {
    const n = buckets[band.key] || 0;
    if (!n) continue;
    const seg = el("span", "rank-strip-seg");
    seg.style.flexGrow = String(n);
    seg.style.background = band.fill;
    strip.appendChild(seg);
  }
  return strip;
}

// ---------------------------------------------------------------- index detail
async function selectTable(table, { silent = false } = {}) {
  if (!table) return;
  state.table = table;
  // the selected index is part of the shareable URL; a click is real navigation, so it
  // gets a history entry, while applying an incoming link does not
  if (!silent) setRoute({ tab: "results", table, section: null }, { push: true });
  state.runLabel = null;
  state.filters = { type: "all", source: "all", failing: false };
  for (const card of document.querySelectorAll(".index-card")) {
    card.classList.toggle("is-on", card.dataset.table === table);
  }
  if (!CACHE.has(table)) {
    CACHE.set(table, await fetch(`data/${table}.json`).then((r) => r.json()));
  }
  renderDetail();
}

function renderDetail() {
  const data = CACHE.get(state.table);
  const host = $("#detail");
  host.replaceChildren();
  const runs = data.runs || [];
  const chosen = runs.find((r) => r.label === state.runLabel) ||
                 runs.find((r) => r.label === "latest") || runs[0];

  const body = block(host, {
    slug: "index",
    eyebrow: "Index detail",
    title: data.index_name,
    lede: `${data.cases.length} golden searches · scored on the top ${data.k} results · matched against ${data.fields.length === 1 && data.fields[0] === "*" ? "every field" : `${data.fields.length} curated fields`}.`,
  });
  body.appendChild(identityRow(data, chosen, runs));

  if (!chosen) {
    body.appendChild(noRunNotice(data));
    goldenSection(host, data);
    fieldSection(host, data);
    return;
  }
  const run = readRun(data, chosen);
  leaderboardSection(body, data, run);
  landingSection(body, data, run);
  tradeoffSection(body, data, run);
  caseTypeSection(body, data, run);
  caseSection(host, data, run);
  legacyGapSection(host, data, run);
  goldenSection(host, data);
  fieldSection(host, data);
}

function identityRow(data, chosen, runs) {
  const wrap = el("div", "ident");
  const fact = (label, value) => {
    const f = el("div", "ident-fact");
    f.appendChild(el("span", "ident-label", label));
    f.appendChild(el("span", "ident-value", value));
    return f;
  };
  const link = el("a", "ident-link", data.index);
  link.href = `https://www.synapse.org/Synapse:${data.index}`;
  link.rel = "noopener";
  const idFact = el("div", "ident-fact");
  idFact.appendChild(el("span", "ident-label", "SearchIndex"));
  idFact.appendChild(link);
  wrap.append(idFact, fact("Golden set", `v${data.version || "—"}`));
  if (chosen) {
    const ran = new Set(Object.values(chosen.per_case || {}).flatMap((pc) => Object.keys(pc)));
    wrap.appendChild(fact("Run", `${chosen.label} · ${day(chosen.run_at)}`));
    wrap.appendChild(fact("Cases scored", ran.size === data.cases.length
      ? `${ran.size} of ${data.cases.length}`
      : `${ran.size} of ${data.cases.length} — ${data.cases.length - ran.size} added since`));
  }
  if (runs.length > 1) {
    wrap.appendChild(segmented({
      label: "Run",
      options: runs.map((r) => ({ value: r.label, label: `${r.label} (${day(r.run_at)})` })),
      value: chosen.label,
      onChange: (v) => { state.runLabel = v; renderDetail(); },
    }));
  }
  return wrap;
}

function noRunNotice(data) {
  const box = el("div", "notice");
  box.appendChild(el("p", "notice-title", "No scored run committed yet"));
  const p = el("p", "notice-body");
  p.append(document.createTextNode(`The golden set is curated and ready — ${data.cases.length} searches with known-correct answers. Score it with `));
  p.appendChild(el("code", null, `python3 benchmark/run.py ${state.table} --label latest`));
  p.append(document.createTextNode(", commit the results, and this section fills in. Or try the searches by hand in the search lab."));
  box.appendChild(p);
  return box;
}

// -- leaderboard ------------------------------------------------------------
function leaderboardSection(host, data, run) {
  const sec = el("div", "sub");
  // One recipe is a set of numbers, not a comparison — a single bar (or a single scatter
  // point) would dress one value up as a chart.
  if (run.keys.length < 2) {
    sec.id = "leaderboard";
    sec.appendChild(subHead(`How ${strategyLabel(run.keys[0]).name} scored`, "leaderboard"));
    sec.appendChild(el("p", "sub-lede",
      `Only one recipe was scored in this run, so there is nothing to rank it against. Score more recipes with benchmark/run.py, or compare recipes live in the search lab.`));
    const tiles = el("div", "hero-figs");
    for (const key of METRIC_KEYS) {
      const t = el("div", "stat stat-neutral");
      t.appendChild(el("p", "stat-label", metricName(key, run.k)));
      t.appendChild(el("p", "stat-value", fmtMetric(key, run.strategies[run.keys[0]][key])));
      t.appendChild(el("p", "stat-sub", METRIC_LABELS[key].help));
      tiles.appendChild(t);
    }
    sec.appendChild(tiles);
    host.appendChild(sec);
    return;
  }
  sec.id = "leaderboard";
  sec.appendChild(subHead("Which recipe ranks best", "leaderboard"));
  sec.appendChild(segmented({
    label: "Rank by",
    options: RANK_BY.map((key) => ({ value: key, label: metricName(key, run.k) })),
    value: state.rankBy,
    onChange: (v) => { state.rankBy = v; renderDetail(); },
  }));
  const { plot } = figure(sec, {
    title: metricName(state.rankBy, run.k),
    note: METRIC_LABELS[state.rankBy].help,
  });
  const dir = METRIC_LABELS[state.rankBy].dir === "up" ? -1 : 1;
  const ordered = [...run.keys].sort((a, b) =>
    ((run.strategies[a][state.rankBy] || 0) - (run.strategies[b][state.rankBy] || 0)) * dir);
  hbar(plot, {
    rows: ordered.map((key) => ({
      label: strategyLabel(key).name,
      value: run.strategies[key][state.rankBy] || 0,
      role: roleOf(key, run),
    })),
    max: isMs(state.rankBy) ? undefined : 1,
    fmt: (v) => fmtMetric(state.rankBy, v),
    unit: metricName(state.rankBy, run.k),
  });
  plot.after(legend([
    { fill: "var(--series-2)", label: "Platform default" },
    ...(run.keys.includes(PRODUCTION_KEY)
      ? [{ fill: "var(--series-3)", label: "In production" }] : []),
    { fill: "var(--series-1)", label: `Best on ${metricName("mrr", run.k)}` },
    { fill: "var(--muted-mark)", label: "Other recipes tested" },
  ]));
  // A held-constant row was measured in a different run, under a different index state.
  // Saying so is the whole point of pinning it rather than leaving the wrong number in.
  for (const [key, g] of Object.entries(run.constants)) {
    plot.parentElement.appendChild(el("p", "fig-note is-constant",
      `${strategyLabel(key).name} is measured on an index with no custom search configuration bound.`));
  }
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — every metric, every recipe",
    head: ["Recipe", ...METRIC_KEYS.map((key) => metricName(key, run.k))],
    align: [null, ...METRIC_KEYS.map(() => "num")],
    rows: ordered.map((key) => [strategyLabel(key).name,
      ...METRIC_KEYS.map((m) => fmtMetric(m, run.strategies[key][m]))]),
  }));
  // The bars are labelled with short names; this is where those names are defined, in the
  // same order the chart plots them so the two can be read side by side.
  plot.parentElement.appendChild(defsTwin({
    summary: "What these recipe names mean",
    items: ordered.map((key) => {
      const l = strategyLabel(key);
      // A recipe can hold two roles at once — production_current winning the run is the
      // outcome worth seeing, and showing only the bar's colour would hide half of it.
      const roles = [];
      if (key === PLATFORM_DEFAULT_KEY) roles.push(ROLE.BASELINE);
      if (key === PRODUCTION_KEY) roles.push(ROLE.PRODUCTION);
      if (key === run.best_key) roles.push(ROLE.BEST);
      return { term: l.name, tag: l.tag, roles, definition: l.blurb, note: l.bestFor };
    }),
  }));
  host.appendChild(sec);
}

// -- rank landings (the signature view) -------------------------------------
function landingSection(host, data, run) {
  const sec = el("div", "sub");
  sec.id = "rank-landings";
  sec.appendChild(subHead("Where the right answer lands", "rank-landings"));
  sec.appendChild(el("p", "sub-lede",
    `A search is only useful if the correct result is somewhere the user will look. For each recipe: how many of the ${ranCaseIds(data, run).length} scored searches put a correct answer at position 1, in the top 3, further down the first page, or nowhere in the top ${run.k}.`));
  const { plot } = figure(sec, { title: `Rank of the first correct result` });
  const ids = ranCaseIds(data, run);
  const ordered = [...run.keys].sort((a, b) =>
    (bucketsFor(run, b, ids).top - bucketsFor(run, a, ids).top));
  const lg = rankstack(plot, {
    rows: ordered.map((key) => ({ label: strategyLabel(key).name, role: roleOf(key, run), buckets: bucketsFor(run, key, ids) })),
    k: run.k,
  });
  plot.after(lg);
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — searches per rank band",
    head: ["Recipe", "Position 1", "Positions 2–3", `Positions 4–${run.k}`, `Not in top ${run.k}`],
    align: [null, "num", "num", "num", "num"],
    rows: ordered.map((key) => {
      const b = bucketsFor(run, key, ids);
      return [strategyLabel(key).name, b.top, b.near, b.deep, b.miss];
    }),
  }));
  host.appendChild(sec);
}

// -- quality vs latency -----------------------------------------------------
function tradeoffSection(host, data, run) {
  if (run.keys.length < 2) return;      // one point is not a trade-off
  const sec = el("div", "sub");
  sec.id = "speed";
  sec.appendChild(subHead("Quality against speed", "speed"));
  const { plot } = figure(sec, {
    title: "MRR vs median round-trip",
    note: "Round-trip is the client-observed wait from query to rendered results, including the async poll. Up and to the left is better.",
  });
  scatter(plot, {
    points: run.keys.map((key) => ({
      x: run.strategies[key].rt_ms_median || 0,
      y: run.strategies[key].mrr || 0,
      label: strategyLabel(key).name,
      role: roleOf(key, run),
    })),
    xLabel: "median ms",
    yLabel: "MRR",
  });
  plot.after(legend([
    { fill: "var(--series-2)", label: "Platform default" },
    { fill: "var(--series-1)", label: "Best on MRR" },
    { fill: "var(--muted-mark)", label: "Other recipes tested" },
  ]));
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — quality and latency",
    head: ["Recipe", "MRR", "Median", "p95"],
    align: [null, "num", "num", "num"],
    rows: run.keys.map((key) => [strategyLabel(key).name, fmtNum(run.strategies[key].mrr),
      fmtMs(run.strategies[key].rt_ms_median), fmtMs(run.strategies[key].rt_ms_p95)]),
  }));
  host.appendChild(sec);
}

// -- known-item vs topical --------------------------------------------------
function caseTypeSection(host, data, run) {
  const types = [...new Set(data.cases.map((c) => c.type).filter(Boolean))];
  if (types.length < 2) return;
  const sec = el("div", "sub");
  sec.id = "search-types";
  sec.appendChild(subHead("Lookups against discovery", "search-types"));
  sec.appendChild(el("p", "sub-lede",
    "Known-item searches have one defensible right answer, so their scores are trustworthy. Topical searches are exploratory — several results are relevant, the golden set lists the ranked head of a larger pool, and recall reads as a floor rather than the whole picture."));
  const pairKeys = [run.baseline_key, run.best_key].filter((k, i, a) => k && a.indexOf(k) === i);
  const rows = [];
  for (const type of types) {
    const ids = data.cases.filter((c) => c.type === type).map((c) => c.id);
    for (const key of pairKeys) {
      rows.push({
        label: `${CASE_TYPE_LABELS[type]?.name || type} · ${key === run.baseline_key ? "today" : "best tested"}`,
        role: roleOf(key, run),
        buckets: bucketsFor(run, key, ids),
        type, key,
        mrr: meanOver(run.per_case[key] || {}, ids, "rr"),
        recall: meanOver(run.per_case[key] || {}, ids, "recall_at_k"),
        n: ids.filter((id) => run.per_case[key]?.[id]).length,
      });
    }
  }
  const { plot } = figure(sec, { title: "Rank of the first correct result, by search type" });
  const lg = rankstack(plot, { rows, k: run.k });
  plot.after(lg);
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — scores by search type",
    head: ["Search type", "Recipe", "Cases", "MRR", `Recall@${run.k}`],
    align: [null, null, "num", "num", "num"],
    rows: rows.map((r) => [CASE_TYPE_LABELS[r.type]?.name || r.type,
      strategyLabel(r.key).name, r.n, fmtNum(r.mrr), fmtNum(r.recall)]),
  }));
  host.appendChild(sec);
}

// -- per-case detail (filters scope this section and the failures below) ----
function filteredCaseIds(data, run) {
  const ran = new Set(ranCaseIds(data, run));
  const baseline = run.per_case[run.baseline_key] || {};
  return data.cases.filter((c) => {
    if (!ran.has(c.id)) return false;
    if (state.filters.type !== "all" && c.type !== state.filters.type) return false;
    if (state.filters.source !== "all" && (c.source || "unattributed") !== state.filters.source) return false;
    if (state.filters.failing) {
      const rank = baseline[c.id]?.first_rel_rank;
      if (rank && rank <= run.k) return false;
    }
    return true;
  }).map((c) => c.id);
}

function caseSection(host, data, run) {
  const body = block(host, {
    slug: "cases",
    eyebrow: "Case level",
    title: "Every search, every recipe",
    lede: `The rank of the first correct result for each curated search. Dark is near the top; grey means nothing correct appeared in the top ${run.k}. The filters scope this table and the failure list below it.`,
  });
  body.appendChild(filterRow(data, run));

  const ids = filteredCaseIds(data, run);
  const byId = new Map(data.cases.map((c) => [c.id, c]));
  if (!ids.length) {
    body.appendChild(el("p", "empty-line", "No searches match these filters."));
    return;
  }
  const { plot } = figure(body, {
    title: `${ids.length} ${ids.length === 1 ? "search" : "searches"} × ${run.keys.length} ${run.keys.length === 1 ? "recipe" : "recipes"}`,
  });
  const lg = heatmap(plot, {
    rowKeys: ids,
    rowLabels: ids.map((id) => byId.get(id).query),
    colKeys: run.keys,
    colLabels: run.keys.map((key) => strategyLabel(key).name),
    colRoles: run.keys.map((key) => roleOf(key, run)),
    cell: (id, key) => run.per_case[key]?.[id]?.first_rel_rank ?? null,
    k: run.k,
  });
  plot.after(lg);
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — rank per search",
    head: ["Search", "Type", ...run.keys.map((key) => strategyLabel(key).name)],
    align: [null, null, ...run.keys.map(() => "num")],
    rows: ids.map((id) => [byId.get(id).query, byId.get(id).type,
      ...run.keys.map((key) => run.per_case[key]?.[id]?.first_rel_rank ?? "miss")]),
  }));
}

function filterRow(data, run) {
  const row = el("div", "filters");
  const types = [...new Set(data.cases.map((c) => c.type).filter(Boolean))];
  row.appendChild(segmented({
    label: "Search type",
    options: [{ value: "all", label: "All" },
      ...types.map((t) => ({ value: t, label: CASE_TYPE_LABELS[t]?.name || t }))],
    value: state.filters.type,
    onChange: (v) => { state.filters.type = v; renderDetail(); },
  }));
  const sources = [...new Set(data.cases.map((c) => c.source || "unattributed"))];
  if (sources.length > 1) {
    const wrap = el("label", "filter-select");
    wrap.appendChild(el("span", "seg-label", "Came from"));
    const sel = el("select");
    for (const s of ["all", ...sources]) {
      const o = el("option", null, s === "all" ? "Any source" : s);
      o.value = s;
      if (s === state.filters.source) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener("change", () => { state.filters.source = sel.value; renderDetail(); });
    wrap.appendChild(sel);
    row.appendChild(wrap);
  }
  const toggle = el("label", "filter-check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = state.filters.failing;
  box.addEventListener("change", () => { state.filters.failing = box.checked; renderDetail(); });
  toggle.append(box, el("span", null, "Only searches today’s default misses"));
  row.appendChild(toggle);
  return row;
}

// -- legacy-search gaps -----------------------------------------------------
/** Cases the LEGACY portal search (the pre-OpenSearch MySQL full-text service) returned
 *  nothing for, scored against what production serves today. Two groups: the ones
 *  OpenSearch closed, and the ones it did not. Flagged per case by
 *  `recall_gap_legacy_search` in the golden set — a hand-recorded historical fact, not
 *  something this harness can re-measure, since the legacy service is gone. */
function legacyGapSection(host, data, run) {
  if (!run.baseline_key) return;
  const byId = new Map(data.cases.map((c) => [c.id, c]));
  const control = run.per_case[run.baseline_key] || {};
  const scoped = filteredCaseIds(data, run).filter((id) => byId.get(id)?.recall_gap_legacy_search);
  const found = (id) => {
    const rank = control[id]?.first_rel_rank;
    return rank && rank <= run.k ? rank : null;
  };
  const wins = scoped.filter((id) => found(id));
  const open = scoped.filter((id) => !found(id));

  const body = block(host, {
    slug: "failures",
    eyebrow: "Legacy search comparison",
    title: "Compared with known legacy search gaps",
    lede: `Previous MySQL portal search gaps compared against "${strategyLabel(run.baseline_key).name}" so gains for moving to OpenSearch are clear.`,
  });
  if (!scoped.length) {
    body.appendChild(el("p", "empty-line", data.cases.some((c) => c.recall_gap_legacy_search)
      ? "No legacy-gap searches match the current filters."
      : "No searches in this golden set are marked as legacy-search gaps, so there is nothing to compare against."));
    return;
  }

  const groups = [
    { ids: wins, cls: "is-win",
      heading: `Closed — ${wins.length} of ${scoped.length} now return a correct result in the top ${run.k}` },
    { ids: open, cls: "is-open",
      heading: `Still missing — ${open.length} of ${scoped.length} remain unanswered` },
  ];
  for (const g of groups) {
    if (!g.ids.length) continue;
    body.appendChild(el("p", `gap-group-head ${g.cls}`, g.heading));
    const grid = el("div", "fail-grid");
    for (const id of g.ids) grid.appendChild(gapCard(byId.get(id), id, run, found(id)));
    body.appendChild(grid);
  }
}

/** One legacy-gap case: the query, its provenance, and where it lands today. */
function gapCard(c, id, run, rank) {
  const card = el("article", `fail-card${rank ? " is-win" : ""}`);
  card.appendChild(el("p", "fail-q", c.query));
  const meta = el("p", "fail-meta");
  if (c.type) meta.appendChild(el("span", "tag", CASE_TYPE_LABELS[c.type]?.name || c.type));
  if (c.expected_pool) meta.appendChild(el("span", "tag is-quiet", `${c.relevant.length} of ~${c.expected_pool} expected`));
  if (c.entity_types?.length) meta.appendChild(el("span", "tag is-quiet", c.entity_types.join(", ")));
  card.appendChild(meta);

  const verdict = el("p", "fail-verdict");
  const dot = el("span", "verdict-dot");
  if (rank) {
    dot.style.background = RANK_BANDS.find((b) => b.key === rankBucket(rank, run.k)).fill;
    verdict.append(dot, document.createTextNode(`Found at position ${rank} today`));
    verdict.classList.add("is-fixable");
  } else {
    // Not closed by the control — but another scored recipe may already do it, which is
    // the difference between "needs tuning" and "needs index-side work".
    const rescue = run.keys
      .map((key) => ({ key, r: run.per_case[key]?.[id]?.first_rel_rank }))
      .filter((x) => x.r && x.r <= run.k)
      .sort((a, b) => a.r - b.r)[0];
    dot.style.background = rescue ? "var(--rank-deep)" : "var(--rank-miss)";
    verdict.append(dot, document.createTextNode(rescue
      ? `Still missing in production — ${strategyLabel(rescue.key).name} finds it at position ${rescue.r}`
      : run.keys.length < 2
        ? "Still missing — only one recipe was scored, try the others in the search lab"
        : "Still missing — no recipe tested finds it, needs index-side work"));
  }
  card.appendChild(verdict);
  if (c.notes) card.appendChild(el("p", "fail-notes", c.notes));
  const foot = el("p", "fail-foot");
  if (c.source) {
    if (c.source_url) {
      const a = el("a", null, c.source);
      a.href = c.source_url;
      a.rel = "noopener";
      foot.appendChild(a);
    } else foot.appendChild(el("span", null, c.source));
  }
  if (c.last_reviewed) foot.appendChild(el("span", "fail-review", `reviewed ${c.last_reviewed}`));
  card.appendChild(foot);
  return card;
}

// -- golden set provenance --------------------------------------------------
function goldenSection(host, data) {
  const body = block(host, {
    slug: "golden-set",
    eyebrow: "Ground truth",
    title: "How this golden set was built",
    lede: "Scores are only as good as the ground truth behind them. Relevant sets are derived from the source table rather than from search results, so a case can expose a gap the search itself has.",
  });
  const counts = new Map();
  for (const c of data.cases) {
    const key = c.source || "unattributed";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const { plot } = figure(body, {
    title: "Where the cases came from",
    note: "Provenance per case, so any score can be traced back to whoever asked for the search.",
  });
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  hbar(plot, {
    rows: rows.map(([label, value]) => ({ label, value, role: ROLE.OTHER })),
    fmt: (v) => String(v),
    unit: "cases",
  });
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — every case",
    head: ["Search", "Type", "Relevant", "Expected pool", "Came from", "Reviewed", "By"],
    align: [null, null, "num", "num", null, null, null],
    rows: data.cases.map((c) => [c.query, c.type, c.relevant.length, c.expected_pool,
      c.source || "unattributed", c.last_reviewed, c.reviewer]),
  }));

  const pooled = data.cases.filter((c) => c.expected_pool);
  if (pooled.length) {
    const listed = pooled.reduce((s, c) => s + c.relevant.length, 0);
    const expected = pooled.reduce((s, c) => s + c.expected_pool, 0);
    body.appendChild(el("p", "fine",
      `${pooled.length} cases carry an SME estimate of how many results truly match. Those cases list ${listed} relevant results against an estimated pool of ${expected}, so their Recall@${data.k} is a floor: a recipe can be penalised for missing results the golden set never named.`));
  }
}

// -- field config -----------------------------------------------------------
function fieldSection(host, data) {
  const body = block(host, {
    slug: "fields",
    eyebrow: "Configuration",
    title: "What the recipes search",
    lede: "Boosts are the main query-time lever this harness exists to test: a match in a field weighted 5 counts five times one weighted 1. Analyzers and synonyms are index-side and need an admin rebuild.",
  });
  if (data.fields.length === 1 && data.fields[0] === "*") {
    body.appendChild(el("p", "empty-line",
      "No curated field list — every recipe searches every field with equal weight. Add benchmark/" + state.table + "/fields.yaml to start weighting them."));
  } else {
    const parsed = data.fields.map((f) => {
      const [field, w] = f.split("^");
      return { label: field, value: w ? Number(w) : 1, role: ROLE.OTHER };
    }).sort((a, b) => b.value - a.value);
    const { plot } = figure(body, { title: "Field boosts in this run" });
    hbar(plot, { rows: parsed, fmt: (v) => `×${v}`, unit: "weight" });
    plot.parentElement.appendChild(tableTwin({
      summary: "Table view — field weights",
      head: ["Field", "Weight"],
      align: [null, "num"],
      rows: parsed.map((r) => [r.label, `×${r.value}`]),
    }));
  }
  if (data.query) {
    const box = el("div", "code-card");
    box.appendChild(el("p", "code-head", "Tuned query shape (fields.yaml)"));
    const pre = el("pre");
    pre.appendChild(el("code", null, JSON.stringify(data.query, null, 2)));
    box.appendChild(pre);
    body.appendChild(box);
  }
}
