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

import { hbar, dumbbell, range, rankstack, heatmap, legend, tableTwin, defsList, rankBucket,
         ROLE, RANK_BANDS, fmtNum, fmtMs } from "./charts.js";
import { strategyLabel, METRIC_LABELS, CASE_TYPE_LABELS, REFERENCE_POINTS } from "./labels.js";
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
/** A metric's definition, with the index's own k filled in. */
const metricText = (key, k) => METRIC_LABELS[key].help.replace("{k}", k);
/** …plus which way is good, for the places with no direction chip beside them (a figure
 *  note, a stat tile). The glossary uses metricText: its chip carries the direction. */
const metricHelp = (key, k) => `${metricText(key, k)} ${METRIC_LABELS[key].dir === "up" ? "Higher" : "Lower"} is better.`;
const pct = (n, d) => (d ? `${Math.round((n / d) * 100)}%` : "—");
const day = (iso) => (iso ? String(iso).slice(0, 10) : "—");

/** A section shell: eyebrow + heading + optional lede, then a body to fill.
 *  `slug` makes the section addressable as #/results/<index>/<slug>. */
function block(host, { eyebrow, title, lede, cta, slug, eyebrowTone }) {
  const sec = el("section", "block");
  if (slug) sec.id = slug;
  const head = el("header", "block-head");
  if (eyebrow) head.appendChild(el("p", `block-eyebrow${eyebrowTone ? ` is-${eyebrowTone}` : ""}`, eyebrow));
  const h = el("h2", null, title);
  if (slug) h.appendChild(anchorLink(slug, { table: state.table }));
  head.appendChild(h);
  if (lede) {
    const p = el("p", "block-lede", lede);
    // The one instruction in the lede is set bold: on a page this dense, "pick an index"
    // is an action, and it reads as prose if it is styled like the sentence around it.
    if (cta) p.appendChild(el("strong", "block-cta", cta));
    head.appendChild(p);
  }
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
    : key === run.promo?.key ? ROLE.PRODUCTION
    : key === run.promo?.stale ? ROLE.OTHER
    : key === PRODUCTION_KEY ? ROLE.PRODUCTION
    : key === PLATFORM_DEFAULT_KEY ? ROLE.BASELINE
    : ROLE.OTHER);

/* Whether what is DEPLOYED is also the best arm tested. Two ways that can be true, and
   the dashboard has to handle both: site.yaml records a promotion (`promoted:` — the only
   way to know an index-side configuration is live, since the repo can only see that a
   config file is committed), or the run's `production_current` arm simply wins. */
function promotionState(data, keys, bestKey) {
  const p = data.promoted;
  if (p?.as && keys.includes(p.as)) {
    return {
      key: p.as, at: p.at, note: p.note,
      isBest: p.as === bestKey, recorded: true,
      /* A promotion can be recorded the moment it goes live, before `production:` is
         re-transcribed and the table re-scored. Then the deployed arm is some experiment
         row — measured as that shape, which is what production now sends — and the run's
         own `production_current` row measures the configuration that was replaced. It has
         to stop claiming production and say what it is instead. */
      stale: p.as !== PRODUCTION_KEY && keys.includes(PRODUCTION_KEY) ? PRODUCTION_KEY : null,
    };
  }
  if (bestKey && bestKey === PRODUCTION_KEY) {
    return { key: PRODUCTION_KEY, at: null, note: null, isBest: true, recorded: false, stale: null };
  }
  return null;
}

/* A label can name a role its mark does not wear. When the deployed arm also wins, its
   bar takes the best fill — but the axis label still carries the deployed tint, so a
   reader scanning the categories can see which row is what production serves. */
const labelRoleOf = (key, run) => (key === run.promo?.key ? ROLE.PRODUCTION : roleOf(key, run));

/** The words for a row that is — or was — production. null when the recipe name says
 *  everything, so only the rows a reader could get wrong are annotated. */
const promoWord = (key, run) => (
  run.promo?.key === key ? "in production"
    : run.promo?.stale === key ? "before promotion"
    : null);
/** The name a figure should label a row with. Once a promotion moves elsewhere, the
 *  `production_current` row is no longer in production, and a chart calling it that is
 *  simply wrong — a rotated heatmap header has no room to explain, so the row takes a
 *  name that states what it now is. The glossary keeps the strategy's own name, being
 *  shared by every index. */
const figureName = (key, run) => (key === run.promo?.stale ? "Pre-promotion" : strategyLabel(key).name);
/** …and `Name · in production` where there is room to mark the deployed row too. */
const rowLabel = (key, run) => (key === run.promo?.key
  ? `${figureName(key, run)} · in production`
  : figureName(key, run));

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
  const best = ranked[0] || null;
  return {
    label: run.label, run_at: run.run_at, k: run.k || data.k,
    strategies, keys, per_case: run.per_case || {},
    // rows pinned to another run (site.yaml `constant:`) — {strategy: {from, run_at}}
    constants: run.constants || {},
    baseline_key: keys.includes(control) ? control : null,
    best_key: best,
    // what is deployed (site.yaml `promoted:`), resolved here so roleOf() and every view
    // read the same answer
    promo: promotionState(data, keys, best),
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

// ------------------------------------------------------------- glossary drawer
/* Non-modal on purpose: the point is to read a definition while looking at the figure it
   came from, so opening it neither traps focus nor blocks the page. Focus does move to
   the panel on open and back to the tab on close, and the panel is inert while shut so it
   stays out of the tab order. */
/* Open/closed is remembered, and reconciled against two other facts: whether this index
   has anything to define, and whether the results tab is the one on screen. syncGlossary
   is the only writer of the DOM state so those three can never disagree. */
const gl = { items: null, onResults: true, want: false };
const GL_KEY = "nf-search-glossary-open";
function wireGlossary() {
  Object.assign(gl, {
    root: $("#glossary"), tab: $("#glossaryTab"), panel: $("#glossaryPanel"),
    close: $("#glossaryClose"), sub: $("#glossarySub"), body: $("#glossaryBody"),
    scrim: $("#glossaryScrim"),
  });
  if (!gl.root) return;
  gl.tab.addEventListener("click", () => openGlossary(!gl.want));
  gl.close.addEventListener("click", () => openGlossary(false));
  gl.scrim.addEventListener("click", () => openGlossary(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && gl.want) openGlossary(false);
  });
  try { gl.want = localStorage.getItem(GL_KEY) === "1"; } catch { gl.want = false; }
  syncGlossary();                       // no focus move: nobody asked for it at load
}

/** Called by app.js when the tab changes. The panel is fixed to the viewport from
 *  <body>, so it cannot ride along with #results being hidden — it has to be told. */
export function glossaryOnTab(tab) {
  gl.onResults = tab === "results";
  syncGlossary();
}

function openGlossary(open, { focus = true } = {}) {
  if (!gl.root) return;
  gl.want = open;
  try { localStorage.setItem(GL_KEY, open ? "1" : "0"); } catch { /* private mode */ }
  syncGlossary();
  if (focus && !gl.root.hidden) (open ? gl.close : gl.tab).focus({ preventScroll: true });
}

/** The one writer of the drawer's DOM state. */
function syncGlossary() {
  if (!gl.root) return;
  const available = gl.onResults && !!gl.items?.length;
  const open = available && gl.want;
  gl.root.hidden = !available;
  gl.root.classList.toggle("is-open", open);
  gl.tab.setAttribute("aria-expanded", String(open));
  gl.panel.inert = !open;                        // shut: out of the tab order entirely
  gl.scrim.hidden = !open;
  gl.scrim.classList.toggle("is-on", open);
}

/** Fill the drawer for the index on screen, or hide the tab when there is nothing to
 *  define. `groups` is [{title, items}] — recipes and metrics are separate lists because
 *  they answer different questions ("what did this row do?" vs "what is this number?").
 *  Content is refreshed even while open, so switching index updates the panel under the
 *  reader rather than closing it, and `want` is left alone so an index with nothing to
 *  define hides the tab without forgetting that the drawer was open. */
function setGlossary(groups, sub) {
  if (!gl.root) return;
  const shown = (groups || []).filter((g) => g.items?.length);
  gl.items = shown;
  syncGlossary();
  if (!shown.length) return;
  gl.sub.textContent = sub || "";
  gl.body.replaceChildren(...shown.flatMap((g) =>
    [el("p", "drawer-group-title", g.title), defsList(g.items)]));
}

/** Plain-language definitions for the scores the figures are labelled with. Same source
 *  as the figure notes (labels.js), so a metric is described one way on the whole site. */
function metricDefs(run) {
  return METRIC_KEYS.map((key) => ({
    term: metricName(key, run.k),
    tag: METRIC_LABELS[key].dir === "up" ? "higher better" : "lower better",
    definition: metricText(key, run.k),
  }));
}

// ---------------------------------------------------------------- boot
export async function initResults(route = {}) {
  MANIFEST = await fetch("data/manifest.json").then((r) => r.json());
  wireGlossary();
  renderHero();
  renderReferencePoints();
  renderPortfolio();
  await showRoute(route, { scroll: true });
  $("#builtStamp").textContent = MANIFEST.generated_at ? `Site built ${MANIFEST.generated_at}.` : "";
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

// ---------------------------------------------------------------- freshness
/* "When was this last updated?" has two answers and they are not the same: when the runs
   were scored (what the numbers describe) and when the site was generated from them (a
   rebuild moves this without any new measurement). The scoring date is the one a reader
   means, so it leads; the build date follows it, and the footer keeps the full timestamp.
   Runs are scored per index; this is the newest, and each index states its own date on its
   card and in its identity row. */
function freshnessStamp() {
  const days = MANIFEST.tables.map((t) => day(t.latest?.run_at)).filter((d) => d && d !== "—").sort();
  const p = el("p", "hero-stamp");
  if (!days.length) {
    p.append(document.createTextNode("No runs published yet"));
    return p;
  }
  p.appendChild(el("span", "hero-stamp-key", "Most recent run"));
  p.append(document.createTextNode(days[days.length - 1]));
  if (MANIFEST.generated_at) {
    p.appendChild(el("span", "hero-stamp-key", "Site built"));
    p.append(document.createTextNode(MANIFEST.generated_at));
  }
  return p;
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
  host.appendChild(freshnessStamp());

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
    ? `${pct(today.top, nToday)} → ${pct(best.top, nBest)} of ${nToday} curated searches, a ${ptLift}-point gain across ${scored.length} of ${MANIFEST.coverage.n_indexes} nf- indexes. Experiments cover both levers: the query, and the index's own configuration.`
    : `${MANIFEST.coverage.n_cases} golden searches are curated across ${MANIFEST.coverage.n_benchmarked} indexes. Run benchmark/run.py and commit the results to fill this in.`));

  if (!nToday) return;

  /* A rail, not three cards. These are context for a headline that already states the
     finding, so they get hairline dividers and no card chrome — the section's only boxes
     should be the ones you can act on. Each item's full explanation is on its `title`;
     the line under the rail carries what the numbers themselves cannot say. */
  const railItem = ({ tone, label, value, notes = [], help }) => {
    const it = el("div", `rail-item${tone ? ` is-${tone}` : ""}`);
    if (help) it.title = help;
    const lab = el("p", "rail-label");
    const dot = el("span", "rail-dot");
    dot.setAttribute("aria-hidden", "true");
    lab.append(dot, document.createTextNode(label));
    it.append(lab, el("p", "rail-value", value));
    for (const n of notes.filter(Boolean)) {
      it.appendChild(el("p", `rail-note${n.tone ? ` is-${n.tone}` : ""}`, n.text));
    }
    return it;
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
  // The headline carries the pooled gain, so the first item answers a different question:
  // is the gain broad, or one index dragging the pool? Counted per index rather than
  // pooled, because an index already at its best is the useful negative result.
  const withRun = scored.filter((t) => t.latest.baseline_key && t.latest.best_key);
  const headroom = withRun.filter((t) =>
    (t.latest.strategies[t.latest.best_key]?.mrr ?? 0) >
    (t.latest.strategies[t.latest.baseline_key]?.mrr ?? 0) + 1e-9);
  const atBest = withRun.filter((t) => !headroom.includes(t));
  const atBestLabel = (t) => t.index_name + (t.promoted?.is_best
    ? ` (promoted${t.promoted.at ? ` ${t.promoted.at}` : ""})` : "");
  const bestGain = headroom.length ? Math.max(...headroom.map((t) =>
    t.latest.strategies[t.latest.best_key].mrr - t.latest.strategies[t.latest.baseline_key].mrr)) : null;

  const rail = el("div", "hero-rail");
  rail.append(
    railItem({
      tone: "best", label: "Indexes with gains", value: `${headroom.length} of ${withRun.length}`,
      notes: [bestGain != null ? { text: `best gain +${fmtNum(bestGain)} MRR` } : null],
      help: "Scored indexes where some tested recipe beats the control on MRR.",
    }),
    railItem({
      tone: "baseline", label: "Searches that miss entirely", value: String(today.miss),
      notes: [
        { text: `${pct(today.miss, nToday)} of ${nToday} curated searches`, tone: "quiet" },
        { text: `best arm misses ${best.miss} (${pct(best.miss, nBest)})`, tone: "quiet" },
      ],
      help: `No correct result anywhere in the top ${scored[0].latest.k} on the platform default.`,
    }),
  );
  if (rtBest != null && rtToday != null) {
    const faster = rtToday - rtBest;
    // as a share of the default, to read the same way as the points gained above
    const shift = Math.round((Math.abs(faster) / rtToday) * 100);
    rail.appendChild(railItem({
      tone: "best", label: "Best recipe latency", value: fmtMs(rtBest),
      notes: [shift < 1 ? null : { text: `${faster > 0 ? "−" : "+"}${shift}% vs the platform default`, tone: faster > 0 ? null : "worse" }],
      help: `Median search-to-results wait, pooled across ${scored.length} indexes; the platform default measures ${fmtMs(rtToday)}.`,
    }));
  }
  host.appendChild(rail);
  host.appendChild(el("p", "hero-rail-note", atBest.length
    ? `Pooled across ${scored.length} scored indexes, measured against the platform default. ${atBest.map(atBestLabel).join(", ")} ${atBest.length === 1 ? "is" : "are"} already at the best arm tested; gains are not uniform.`
    : `Pooled across ${scored.length} scored indexes, measured against the platform default. Every one has a tested recipe that beats its control.`));
}

// ------------------------------------------------------- reference points (reading guide)
/* Three arms recur in every figure below, and their names are only obvious to someone who
   already knows the codebase. Stated once, up front, in experiment terms — control /
   deployed / best tested — so a reader meets them before the first chart uses them, and
   in the same colours the marks wear. Copy lives in labels.js beside the strategy blurbs.

   Laid out as the lifecycle rather than as three loose definitions, because the arms are
   one sequence: NF moved off the platform default by deploying a configuration, and a
   best arm is what a future deployment would promote. Card order still matches the order
   the charts plot them, so the two connectors run in opposite directions — settled
   transitions grey and solid, the prospective one teal and dashed. */
const FLOW_STEPS = [
  { label: "deployed as", back: false },      // platform default -> in production
  { label: "promotion candidate", back: true },  // best experiment -> in production
];

/** A connector between two stage cards. Decorative arrow, meaningful label: the label
 *  alone has to read sensibly in DOM order, since the direction is carried visually. */
function flowConnector({ label, back }) {
  const c = el("div", `flow-arrow${back ? " is-back" : ""}`);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 40 16");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = '<path class="flow-shaft" d="M2 8h30"/><path d="M27.5 3.5 32 8l-4.5 4.5"/>';
  c.append(svg, el("span", "flow-arrow-label", label));
  return c;
}

function renderReferencePoints() {
  const host = $("#reference");
  host.replaceChildren();
  const body = block(host, {
    slug: "reference",
    eyebrow: "How to read this",
    title: "Three reference points",
    lede: `Every recipe is an arm of one experiment, scored over the same searches with known answers. Three arms are named, and carry these colours in every figure.`,
  });

  const track = el("div", "flow-track");
  track.setAttribute("role", "group");
  track.setAttribute("aria-label", "Search configuration lifecycle: platform default, in production, best experiment");
  REFERENCE_POINTS.forEach((rp, i) => {
    if (i > 0) track.appendChild(flowConnector(FLOW_STEPS[i - 1]));
    // Collapsed, a node is a legend entry: colour, role, name, one clause. The prose a
    // first-time reader needs is one click away rather than three paragraphs down the
    // page — the flow and the colour mapping are what have to be always visible.
    // rp.tone is the chart ROLE, so the node's rail and dot are coloured by the same
    // stylesheet rules that colour the marks — no second copy of the mapping here.
    const node = el("details", `flow-node is-${rp.tone}`);
    const sum = document.createElement("summary");
    const head = el("div", "ref-head");
    const dot = el("span", "ref-dot");
    dot.setAttribute("aria-hidden", "true");
    head.append(dot, el("span", "ref-role", rp.role));
    const text = el("div", "flow-node-text");
    text.append(head, el("h3", "ref-name", rp.name), el("p", "flow-node-gist", rp.gist));
    const chev = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chev.setAttribute("class", "disclosure-icon");
    chev.setAttribute("viewBox", "0 0 24 24");
    chev.setAttribute("aria-hidden", "true");
    chev.innerHTML = '<path d="M9 6l6 6-6 6"/>';
    sum.append(text, chev);
    const bodyEl = el("div", "flow-node-body");
    // the exact words the figures use, so a node is findable from a legend and back
    const aka = el("p", "ref-aka");
    aka.appendChild(el("span", "ref-aka-label", "In figures"));
    aka.append(document.createTextNode(rp.aka));
    const why = el("p", "ref-why");
    why.appendChild(el("span", "ref-why-label", "Interpretation"));
    why.append(document.createTextNode(rp.why));
    bodyEl.append(aka, el("p", "ref-what", rp.what), why, el("p", "ref-caveat", rp.caveat));
    node.append(sum, bodyEl);
    track.appendChild(node);
  });
  body.appendChild(track);

  // The arrows state the direction; this states it in words, for anyone reading the cards
  // as a list. Both connectors point at production because that is the deployed state.
  body.appendChild(el("p", "flow-note",
    "Both arrows point at production: the platform default is what a configuration replaced, and a best arm is what the next promotion would deploy. Nothing on this page changes what users see."));

  const nConfigured = MANIFEST.coverage?.n_configured;
  const promotedTables = MANIFEST.tables.filter((t) => t.promoted?.is_best);
  const fine = el("p", "fine");
  if (nConfigured != null) {
    fine.append(document.createTextNode(
      `${nConfigured} of ${MANIFEST.coverage.n_indexes} nf- indexes have a custom search config; on the other ${MANIFEST.coverage.n_indexes - nConfigured}, “In production” and the platform default are the same query. `));
  }
  if (promotedTables.length) {
    // the dashed arrow has already been walked on these indexes — the two right-hand
    // nodes are one arm there, which is why they show no gain
    fine.append(document.createTextNode(
      `On ${promotedTables.length} of the ${MANIFEST.tables.filter((t) => t.latest).length} scored indexes the deployed arm is already the best tested.`));
  }
  if (fine.childNodes.length) body.appendChild(fine);
}

// ---------------------------------------------------------------- portfolio
function renderPortfolio() {
  const host = $("#portfolio");
  host.replaceChildren();
  const body = block(host, {
    slug: "portfolio",
    eyebrow: "Portfolio",
    title: "Every NF search index",
    lede: `${MANIFEST.coverage.n_benchmarked} of ${MANIFEST.coverage.n_indexes} nf- indexes have a golden set; ${MANIFEST.coverage.n_configured} have a custom search config. `,
    cta: "Pick an index to see its results in detail.",
  });

  /* Only an index you can open gets a card. An index with no golden set has nothing to
     show and nothing to select, so eight of them as cards made the picker the loudest
     thing on the page — they become a chip list instead, which still states coverage. */
  const cards = el("div", "index-cards");
  const unscored = [];
  for (const row of MANIFEST.registry) {
    const t = row.table ? MANIFEST.tables.find((x) => x.table === row.table) : null;
    if (!t) { unscored.push(row); continue; }
    const card = el("button", "index-card");
    card.type = "button";
    card.dataset.table = t.table;
    card.addEventListener("click", () => selectTable(t.table));
    card.appendChild(el("p", "ic-name", row.index_name));
    const meta = el("p", "ic-meta");
    meta.appendChild(el("span", "ic-id", row.index));
    if (t.promoted) {
      // Supersedes the "config" chip: a promoted index has one by definition, and two
      // chips beside the synID do not fit the card. The distinction the chip draws is the
      // one that matters — committed vs actually deployed.
      const flag = el("span", "ic-flag is-promoted", "promoted");
      flag.title = `${strategyLabel(t.promoted.as).name} is deployed${t.promoted.at ? ` from ${t.promoted.at}` : ""}`
        + (t.promoted.is_best ? " and is the best arm in this run." : ".");
      meta.appendChild(flag);
    } else if (row.configured) {
      meta.appendChild(el("span", "ic-flag", "config"));
    }
    card.appendChild(meta);
    if (t.latest) {
      const b = t.latest.buckets[t.latest.baseline_key];
      const n = b ? Object.values(b).reduce((s, v) => s + v, 0) : 0;
      card.appendChild(el("p", "ic-stat", n ? `${pct(b.top, n)} answered first` : "run committed"));
      if (b) card.appendChild(rankStrip(b, n));
      card.appendChild(el("p", "ic-sub", `${t.n_cases} cases · run ${day(t.latest.run_at)}`));
    } else {
      card.appendChild(el("p", "ic-stat is-quiet", "not scored yet"));
      card.appendChild(el("p", "ic-sub", `${t.n_cases} golden cases ready`));
    }
    cards.appendChild(card);
  }
  body.appendChild(cards);

  if (unscored.length) {
    const band = el("p", "index-chips");
    band.appendChild(el("span", "chips-label", `No golden set yet (${unscored.length})`));
    for (const row of unscored) {
      const chip = el("span", `chip${row.configured ? " has-config" : ""}`, row.index_name);
      // the card carried the synID and the config flag; the chip carries them on hover
      chip.title = row.configured ? `${row.index} · has a custom search config` : row.index;
      band.appendChild(chip);
    }
    body.appendChild(band);
  }

  // The cards carry rank strips, which are the same four bands the per-search-type figure
  // plots — so they get the same legend rather than leaving the colours unexplained.
  // k is per-index in principle; only state a number when every scored index agrees.
  const ks = [...new Set(MANIFEST.tables.map((t) => t.latest?.k).filter(Boolean))];
  const kLabel = ks.length === 1 ? String(ks[0]) : "k";
  const anyStrip = MANIFEST.tables.some((t) => t.latest?.buckets?.[t.latest.baseline_key]);
  if (anyStrip) {
    body.appendChild(legend(
      RANK_BANDS.map((b) => ({ fill: b.fill, label: b.label.replace("{k}", kLabel) })),
      "Each bar above: the position of the first correct result, across that index's golden cases.",
    ));
  }

  // baseline -> best, per index that has a run
  const scored = MANIFEST.tables.filter((t) => t.latest?.baseline_key && t.latest?.best_key);
  if (scored.length) {
    const { plot } = figure(body, {
      title: "Where each index stands",
      note: "Position of the first correct result, as mean reciprocal rank. Left dot is the platform default, right dot the best recipe tested.",
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
        strategyLabel(t.latest.best_key).name + (t.promoted?.is_best ? " · in production" : ""),
        day(t.latest.run_at)]),
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
    const on = card.dataset.table === table;
    card.classList.toggle("is-on", on);
    // the cards are a picker for the detail section below; say so to assistive tech too
    if (card.dataset.table) card.setAttribute("aria-current", on ? "true" : "false");
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
    eyebrow: "Selected index",
    eyebrowTone: "current",
    title: data.index_name,
    lede: `${data.cases.length} golden searches · scored on the top ${data.k} results · matched against ${data.fields.length === 1 && data.fields[0] === "*" ? "every field" : `${data.fields.length} curated fields`}.`,
  });
  body.appendChild(identityRow(data, chosen, runs));

  if (!chosen) {
    body.appendChild(noRunNotice(data));
    setGlossary(null);                  // nothing scored, so nothing to define
    goldenSection(host, data);
    fieldSection(host, data);
    return;
  }
  const run = readRun(data, chosen);
  // "What is deployed is already the best arm tested" is this index's headline finding,
  // so it is stated above the leaderboard rather than left for a reader to infer from a
  // bar that happens to be both the control and the winner.
  if (run.promo?.isBest) body.appendChild(promotedBanner(data, run, run.promo));
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
  if (data.promoted?.at) wrap.appendChild(fact("In production", data.promoted.at));
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

/** The promoted-state callout: what is running, since when, and that nothing tested
 *  beats it. Deliberately two lines — the caveats about what each row measures belong
 *  with the figure they are about, not stacked under a headline. */
function promotedBanner(data, run, promo) {
  const box = el("div", "notice is-promoted");
  const title = el("p", "notice-title");
  const mark = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  mark.setAttribute("class", "notice-mark");
  mark.setAttribute("viewBox", "0 0 24 24");
  mark.setAttribute("aria-hidden", "true");
  mark.innerHTML = '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.5 2.5L16 9.5"/>';
  title.append(mark, document.createTextNode("What is deployed is already the best arm tested"));
  box.appendChild(title);

  const p = el("p", "notice-body");
  const name = strategyLabel(promo.key).name;
  const when = promo.at ? `serves from ${promo.at}` : "serves today";
  p.append(document.createTextNode(promo.recorded
    ? `${name} is the row that measures what ${data.index_name} ${when}. No other arm in this run scores higher on ${metricName("mrr", run.k)}.`
    : `${name} is what ${data.index_name} serves today, and it wins this run: no other arm scores higher on ${metricName("mrr", run.k)}.`));
  box.appendChild(p);

  return box;
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
  const promo = run.promo;
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
      t.appendChild(el("p", "stat-sub", metricHelp(key, run.k)));
      tiles.appendChild(t);
    }
    sec.appendChild(tiles);
    // one recipe needs no list to tell it from others, but its six scores still do
    setGlossary([{ title: "Metrics", items: metricDefs(run) }],
                `What each score reported for ${data.index_name} means.`)
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
    note: metricHelp(state.rankBy, run.k),
  });
  const dir = METRIC_LABELS[state.rankBy].dir === "up" ? -1 : 1;
  const ordered = [...run.keys].sort((a, b) =>
    ((run.strategies[a][state.rankBy] || 0) - (run.strategies[b][state.rankBy] || 0)) * dir);
  hbar(plot, {
    rows: ordered.map((key) => ({
      label: figureName(key, run),
      value: run.strategies[key][state.rankBy] || 0,
      role: roleOf(key, run),
      labelRole: labelRoleOf(key, run),
    })),
    max: isMs(state.rankBy) ? undefined : 1,
    fmt: (v) => fmtMetric(state.rankBy, v),
    unit: metricName(state.rankBy, run.k),
  });
  // Only legend a colour some bar actually wears. An index whose control also wins has no
  // baseline-coloured bar, and listing one anyway sends the reader hunting for it.
  const rolesShown = new Set(run.keys.map((key) => roleOf(key, run)));
  plot.after(legend([
    ...(rolesShown.has(ROLE.BASELINE) ? [{ fill: "var(--series-2)", label: "Platform default" }] : []),
    ...(rolesShown.has(ROLE.PRODUCTION) ? [{ fill: "var(--series-3)", label: "In production" }] : []),
    { fill: "var(--series-1)", label: `Best on ${metricName("mrr", run.k)}` },
    ...(rolesShown.has(ROLE.OTHER) ? [{ fill: "var(--muted-mark)", label: "Other recipes tested" }] : []),
  ]));
  // A promotion that no longer wins is the ordinary state after new experiments, and the
  // chart cannot show it: the deployed row is just another bar. Name it.
  if (promo && !promo.isBest) {
    plot.parentElement.appendChild(el("p", "fig-note is-constant",
      `${strategyLabel(promo.key).name} is what this index serves${promo.at ? ` from ${promo.at}` : " today"}; ${strategyLabel(run.best_key).name} scores higher in this run.`));
  }
  if (promo?.stale) {
    plot.parentElement.appendChild(el("p", "fig-note is-constant",
      `${figureName(promo.stale, run)} is the configuration in place before ${promo.at || "the promotion"} — what the portal sent until then, not what it sends now.`));
  }
  // The row is labelled and coloured as the platform default, and on an index whose own
  // configuration is live it is not a platform-default measurement.
  if (promo && promo.key === PLATFORM_DEFAULT_KEY) {
    plot.parentElement.appendChild(el("p", "fig-note is-constant",
      `${strategyLabel(promo.key).name} is the default query, but this index runs a custom configuration, so the row includes it — not a platform-default measurement.`));
  }
  if (promo?.note) plot.parentElement.appendChild(el("p", "fig-note is-constant", promo.note));
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
    rows: ordered.map((key) => [figureName(key, run),
      ...METRIC_KEYS.map((m) => fmtMetric(m, run.strategies[key][m]))]),
  }));
  // The bars are labelled with short names; the drawer defines them, in the same order
  // the chart plots them so the two can be read side by side.
  const recipeDefs = ordered.map((key) => {
      const l = strategyLabel(key);
      // One badge only: every other role a row can hold is already its name, and this
      // block is shared by every index — which recipe won THIS run is the one thing the
      // term cannot say.
      const roles = [];
      if (key === run.best_key) roles.push(ROLE.BEST);
      // same tint the plot gives this recipe's category label, including the deployed
      // arm on a promoted index — colour is how an entry ties back to its mark
    return { term: l.name, tag: l.tag, roles, termRole: labelRoleOf(key, run),
             definition: l.blurb, note: l.bestFor };
  });
  setGlossary(
    [{ title: "Recipes", items: recipeDefs }, { title: "Metrics", items: metricDefs(run) }],
    `The ${ordered.length} recipes scored on ${data.index_name}, in the order the leaderboard ranks them, and what each score means.`,
  );
  host.appendChild(sec);
}

// -- rank landings (the signature view) -------------------------------------
function landingSection(host, data, run) {
  const sec = el("div", "sub");
  sec.id = "rank-landings";
  sec.appendChild(subHead("How high correct results rank", "rank-landings"));
  sec.appendChild(el("p", "sub-lede",
    `The rank of the first correct result, per recipe, across ${ranCaseIds(data, run).length} scored searches: position 1, the top 3, further down, or nowhere in the top ${run.k}.`));
  const { plot } = figure(sec, { title: `Rank of the first correct result` });
  const ids = ranCaseIds(data, run);
  const ordered = [...run.keys].sort((a, b) =>
    (bucketsFor(run, b, ids).top - bucketsFor(run, a, ids).top));
  const lg = rankstack(plot, {
    rows: ordered.map((key) => ({ label: figureName(key, run), role: roleOf(key, run),
                                  labelRole: labelRoleOf(key, run), buckets: bucketsFor(run, key, ids) })),
    k: run.k,
  });
  plot.after(lg);
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — searches per rank band",
    head: ["Recipe", "Position 1", "Positions 2–3", `Positions 4–${run.k}`, `Not in top ${run.k}`],
    align: [null, "num", "num", "num", "num"],
    rows: ordered.map((key) => {
      const b = bucketsFor(run, key, ids);
      return [figureName(key, run), b.top, b.near, b.deep, b.miss];
    }),
  }));
  host.appendChild(sec);
}

// -- quality vs latency -----------------------------------------------------
function tradeoffSection(host, data, run) {
  if (run.keys.length < 2) return;      // one row is not a comparison
  const sec = el("div", "sub");
  sec.id = "speed";                     // unchanged: #/results/<index>/speed still resolves
  sec.appendChild(subHead("How long a search takes", "speed"));
  sec.appendChild(el("p", "sub-lede",
    "Round-trip is the wait from query to rendered results, measured in the browser and including the poll. It is mostly a property of the index and the network rather than of the query shape."));

  // Ordered by MRR, best at top, so "does the winning recipe cost anything?" is read off
  // the row order. Quality is plotted in the leaderboard; repeating it on a second axis
  // implied a frontier that the measurements do not support — the medians of every
  // recipe on an index are typically within a few percent of each other.
  const ordered = [...run.keys].sort((a, b) =>
    (run.strategies[b].mrr || 0) - (run.strategies[a].mrr || 0));
  /* Where the axis starts. Every recipe on an index waits on the same network and the
     same async poll, so the interesting range is a band well above zero — anchoring at
     zero spends half the plot on time nobody measured. Rounded down to a 250 ms step
     below the fastest mark, with a margin so the leftmost dot never sits on the axis:
     1000 ms on nf-tools, 500 ms on the sub-second indexes. */
  const marks = ordered.flatMap((key) => [run.strategies[key].rt_ms_median, run.strategies[key].rt_ms_p95])
    .filter((v) => typeof v === "number");
  const lo = Math.min(...marks), hi = Math.max(...marks);
  const axisFrom = Math.max(0, Math.floor((lo - (hi - lo) * 0.08) / 250) * 250);
  const { plot } = figure(sec, {
    title: `Round-trip time, median to 95th percentile`,
    note: "One row per recipe, ordered by MRR: solid dot the median wait, hollow dot the 95th percentile.",
  });
  range(plot, {
    min: axisFrom,
    rows: ordered.map((key) => ({
      label: rowLabel(key, run),
      from: run.strategies[key].rt_ms_median || 0,
      to: run.strategies[key].rt_ms_p95 || 0,
      role: roleOf(key, run),
      labelRole: labelRoleOf(key, run),
    })),
    fmt: fmtMs,
    // the unit once per row, on the value the eye lands on
    fmtShort: (v) => String(Math.round(v)),
    fromLabel: "median",
    toLabel: "95th percentile",
  });
  const rolesShown = new Set(run.keys.map((key) => roleOf(key, run)));
  plot.after(legend([
    ...(rolesShown.has(ROLE.BASELINE) ? [{ fill: "var(--series-2)", label: "Platform default" }] : []),
    ...(rolesShown.has(ROLE.PRODUCTION) ? [{ fill: "var(--series-3)", label: "In production" }] : []),
    { fill: "var(--series-1)", label: `Best on ${metricName("mrr", run.k)}` },
    ...(rolesShown.has(ROLE.OTHER) ? [{ fill: "var(--muted-mark)", label: "Other recipes tested" }] : []),
  ]));

  /* State the spread as a number. On most indexes the medians sit within a few percent of
     each other, and "there is nothing to choose here" is a finding a reader should not
     have to infer from a plot that looks like a comparison. */
  const meds = ordered.map((key) => run.strategies[key].rt_ms_median).filter((v) => typeof v === "number");
  if (meds.length > 1) {
    const lo = Math.min(...meds), hi = Math.max(...meds);
    const sorted = [...meds].sort((a, b) => a - b);
    const mid = sorted.length % 2
      ? sorted[(sorted.length - 1) / 2]
      : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
    const share = Math.round(((hi - lo) / mid) * 100);
    const slowest = ordered.find((key) => run.strategies[key].rt_ms_median === hi);
    const fastest = ordered.find((key) => run.strategies[key].rt_ms_median === lo);
    plot.parentElement.appendChild(el("p", "fig-note",
      `Median round-trip differs by ${Math.round(hi - lo)} ms across ${meds.length} recipes — ${share}% of ${fmtMs(mid)}. `
      + (share < 10
        ? "Median round-trip does not separate the recipes."
        : `Slowest is ${figureName(slowest, run)} at ${fmtMs(hi)}, fastest ${figureName(fastest, run)} at ${fmtMs(lo)}.`)));
  }

  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — quality and latency",
    head: ["Recipe", metricName("mrr", run.k), "Median", "p95"],
    align: [null, "num", "num", "num"],
    rows: ordered.map((key) => [figureName(key, run), fmtNum(run.strategies[key].mrr),
      fmtMs(run.strategies[key].rt_ms_median), fmtMs(run.strategies[key].rt_ms_p95)]),
  }));
  host.appendChild(sec);
}

function caseTypeSection(host, data, run) {
  const types = [...new Set(data.cases.map((c) => c.type).filter(Boolean))];
  if (types.length < 2) return;
  const sec = el("div", "sub");
  sec.id = "search-types";
  sec.appendChild(subHead("Known-item against topical searches", "search-types"));
  sec.appendChild(el("p", "sub-lede",
    "Known-item searches have one defensible right answer, so their scores are trustworthy. Topical searches are exploratory: several results are relevant and the golden set lists only the head of a larger pool, so recall reads as a floor."));
  /* Which arms to pair up per type. Without a promotion this is the control against the
     best tested — "today" vs what could be. Once a promotion is recorded, "today" is no
     longer the control: the deployed arm is what production serves and the superseded row
     is what it replaced, so the pair becomes before-and-after and the labels have to say
     so. A promoted arm that lost adds a third row, which is then the whole story. */
  const pairKeys = [run.promo?.stale || run.baseline_key, run.promo?.key, run.best_key]
    .filter((k, i, a) => k && a.indexOf(k) === i);
  const roleWord = (key) => promoWord(key, run)
    || (key === run.baseline_key ? "today" : "best tested");
  const rows = [];
  for (const type of types) {
    const ids = data.cases.filter((c) => c.type === type).map((c) => c.id);
    for (const key of pairKeys) {
      rows.push({
        label: `${CASE_TYPE_LABELS[type]?.name || type} · ${roleWord(key)}`,
        role: roleOf(key, run),
        labelRole: labelRoleOf(key, run),
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
  if (run.promo?.stale) {
    plot.parentElement.appendChild(el("p", "fig-note",
      `Before and after the promotion, per search type: the pair is the configuration production replaced against the one it serves${run.promo.at ? ` from ${run.promo.at}` : ""}.`));
  }
  // the type split of a single arm — nothing beat it, so there is no pair to draw
  if (pairKeys.length === 1) {
    plot.parentElement.appendChild(el("p", "fig-note",
      `Single arm: ${figureName(pairKeys[0], run)} is both what this index serves and the best tested, so there is no comparison row.`));
  }
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — scores by search type",
    head: ["Search type", "Recipe", "Cases", "MRR", `Recall@${run.k}`],
    align: [null, null, "num", "num", "num"],
    rows: rows.map((r) => [CASE_TYPE_LABELS[r.type]?.name || r.type,
      `${figureName(r.key, run)} (${roleWord(r.key)})`, r.n, fmtNum(r.mrr), fmtNum(r.recall)]),
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
    lede: `Rank of the first correct result per search. Dark is near the top; grey means nothing correct in the top ${run.k}. The filters scope this table and the failure list below.`,
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
    colLabels: run.keys.map((key) => figureName(key, run)),
    colRoles: run.keys.map((key) => roleOf(key, run)),
    colLabelRoles: run.keys.map((key) => labelRoleOf(key, run)),
    cell: (id, key) => run.per_case[key]?.[id]?.first_rel_rank ?? null,
    k: run.k,
  });
  plot.after(lg);
  plot.parentElement.appendChild(tableTwin({
    summary: "Table view — rank per search",
    head: ["Search", "Type", ...run.keys.map((key) => figureName(key, run))],
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
    lede: `Gaps in the previous MySQL portal search, measured against ${figureName(run.baseline_key, run)} — what moving to OpenSearch closed.`,
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
const REPO = "https://github.com/nf-osi/opensearch-ops";
const GH_MARK = '<svg class="gh-mark" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">'
  + '<path d="M12 .5C5.73.5.5 5.73.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79-.25.79-.55v-2.1'
  + 'c-3.2.7-3.88-1.54-3.88-1.54-.52-1.34-1.28-1.7-1.28-1.7-1.05-.72.08-.7.08-.7 1.16.08 1.77 '
  + '1.19 1.77 1.19 1.03 1.76 2.7 1.25 3.36.96.1-.75.4-1.25.73-1.54-2.55-.29-5.24-1.28-5.24-5.68'
  + ' 0-1.25.45-2.28 1.19-3.08-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11.1 11.1 0 0 1 5.8'
  + ' 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.8 1.19 1.83 1.19 3.08 0 4.41-2.69'
  + ' 5.39-5.25 5.67.41.36.77 1.06.77 2.14v3.17c0 .3.21.67.8.55C20.71 21.39 24 17.08 24 12 24 5.73'
  + ' 18.27.5 12 .5z"/></svg>';

/** Two ways to contribute, because the audience splits. An issue needs no git and no
 *  YAML — it is the path for the person who knows what the search should find. The edit
 *  link opens golden.yaml in GitHub's editor, which forks and raises the PR for anyone
 *  who would rather write the case themselves. Both land in the same review. */
function contributeRow(data) {
  const wrap = el("div", "contrib");
  const q = (o) => Object.entries(o).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const issue = `${REPO}/issues/new?` + q({
    title: `Benchmark (${data.index_name}): propose a search`,
    body: [
      "### The search",
      "What would a researcher type? (Or name the existing case you want revised.)",
      "",
      "### What it should find",
      "The results that ought to come back — names, RRIDs or Synapse ids.",
      "",
      "### How you know",
      "A paper, a link, or the reasoning. This is what makes the case defensible.",
      "",
      "---",
      `Index: ${data.index_name} (${data.index})`,
      `Golden set: benchmark/${state.table}/golden.yaml`,
      "Opened from the benchmark dashboard.",
    ].join("\n"),
  });
  const link = (href, label, cls) => {
    const a = el("a", cls);
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener";
    a.innerHTML = GH_MARK;                        // static markup, no interpolation
    a.append(document.createTextNode(label));
    return a;
  };
  wrap.append(
    link(issue, "Propose a search or a fix", "contrib-btn"),
    link(`${REPO}/edit/main/benchmark/${state.table}/golden.yaml`, "Edit the golden set", "contrib-btn is-quiet"),
  );
  return wrap;
}

function goldenSection(host, data) {
  const body = block(host, {
    slug: "golden-set",
    eyebrow: "Ground truth",
    title: "How this golden set was built",
    lede: "Relevant sets are derived from the source table rather than from search results, so a case can expose a gap in the search itself.",
  });
  const counts = new Map();
  for (const c of data.cases) {
    const key = c.source || "unattributed";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const { plot } = figure(body, {
    title: "Where the cases came from",
    note: "Provenance per case, so a score traces back to whoever asked for the search.",
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
      `${pooled.length} cases carry an SME estimate of how many results truly match: ${listed} named against an estimated pool of ${expected}. Their Recall@${data.k} is a floor — a recipe can be penalised for missing results the golden set never named.`));
  }
  body.appendChild(contributeRow(data));
}

// -- field config -----------------------------------------------------------
function fieldSection(host, data) {
  const body = block(host, {
    slug: "fields",
    eyebrow: "Configuration",
    title: "What the recipes search",
    lede: "The main query-time lever: a match in a field weighted 5 counts five times one weighted 1. Analyzers and synonyms are index-side and require an admin rebuild.",
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
