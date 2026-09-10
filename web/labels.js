// Audience-facing copy for business owners / SMEs. Strategy and metric prose is lifted
// from benchmark/tools/RESULTS.md (the stakeholder-facing summary) so the site and the
// engineer-facing doc say the same thing. Keep names/wording in sync with that doc.

// Friendly name + one-line "what it does" + "best for" for each strategy key.
// `tag` is the short chip shown in compact UI.
export const STRATEGY_LABELS = {
  frontend_default: {
    name: "Platform default",
    tag: "default",
    blurb: "The portal default where nothing is customized: the Synapse front-end's built-in query — every field, equal weight, typo tolerance, no boosts — on an index with no search configuration bound, so no custom analyzers or synonym sets either.",
    bestFor: "The control every recipe here is measured against — a platform-wide fallback, not a choice NF made.",
  },
  production_current: {
    name: "In production",
    tag: "live",
    blurb: "The query this portal page actually issues right now, transcribed from its custom search config — including the rule that sends quoted phrases and Synapse ids down a separate exact-match path.",
    bestFor: "The control for an index whose portal has customized its search: any gain a recipe shows here is a gain over what users get today, not over a default nobody runs.",
  },
  simple_query_string_boosted: {
    name: "Google-style (boosted)",
    tag: "simple+",
    blurb: "The forgiving operator-aware query, but with our field boosts applied. This is the exact shape production falls back to for a quoted phrase or a Synapse id.",
    bestFor: "Exact-phrase and identifier lookups, where typo tolerance does more harm than good.",
  },
  simple_query_string: {
    name: "Google-style",
    tag: "simple",
    blurb: "A forgiving query across the curated fields, equally weighted. Supports operators a user might type (+, -, quotes).",
    bestFor: "A safe general-purpose default.",
  },
  multi_match_best: {
    name: "Best field",
    tag: "best",
    blurb: "Searches the curated fields equally and scores each result by its single best-matching field.",
    bestFor: "General search where the strongest single signal should win.",
  },
  multi_match_boosted: {
    name: "Boosted by name",
    tag: "boosted",
    blurb: "Best-field matching plus our boosts (name, synonyms, RRID count more than description) so the obvious canonical match rises.",
    bestFor: "Pushing the obvious canonical match to the top.",
  },
  multi_match_cross: {
    name: "Cross fields (boosted)",
    tag: "cross",
    blurb: "Treats the searched fields as one combined field, so a query whose words are spread across several fields still matches well. Uses the same boosts.",
    bestFor: "Queries where terms are scattered across fields. (Strongest overall in the benchmark.)",
  },
  boosted_fuzzy: {
    name: "Typo-tolerant",
    tag: "fuzzy",
    blurb: "The boosted strategy plus typo tolerance, so “schwan” still finds “Schwann”.",
    bestFor: "Misspellings and near-misses (adds noise on identifier-heavy data).",
  },
  phrase_prefix: {
    name: "As-you-type",
    tag: "typeahead",
    blurb: "Treats the last word as a prefix, like live typeahead (“neurofib…” matches “neurofibromin”).",
    bestFor: "Autocomplete / as-you-type search boxes.",
  },
};

// The three reference points every figure on the results page is read against, named as
// arms of an experiment rather than in search-engine terms. `tone` is the ROLE
// (charts.js) whose colour the arm's mark wears in every chart; `gist` is the one clause
// the collapsed card shows; `aka` is the wording the figures use, so a card can be found
// from a legend. Order is control -> deployed ->
// best, which is also the order the charts plot them.
export const REFERENCE_POINTS = [
  {
    tone: "baseline",
    gist: "no custom config",
    role: "Control arm",
    name: "Platform default",
    aka: "“Platform default”; “today” in two-point figures",
    what: "The portal default for any portal with no custom config — no query tuning, and no index-side settings such as custom analyzers or synonyms. All fields, equal weight, typo tolerance, no boosts.",
    why: "The fixed control. Gains on this page are measured against it unless a figure says otherwise.",
    caveat: "Present on every index. A platform fallback, not a choice NF made.",
  },
  {
    tone: "production",
    gist: "what users get now",
    role: "Deployed arm",
    name: "In production",
    aka: "“In production”; “portal today” in glossaries",
    what: "The query this portal issues today, transcribed from this index’s custom search config. Quoted phrases and Synapse ids take a separate exact-match path.",
    why: "What users get now. A recipe above it would change production results; one below it would be a regression if promoted.",
    caveat: "Only on indexes with a custom search config. Elsewhere it is the same query as the platform default.",
  },
  {
    tone: "best",
    gist: "highest MRR this run",
    role: "Best arm tested",
    name: "Best experiment",
    aka: "“Best recipe tested”; “Best on MRR” in single-metric figures",
    what: "The highest-MRR arm in this run. Arms vary in query shape, in field boosts, and in the index’s own search configuration.",
    why: "The measured upper bound so far, and the promotion candidate. Not deployed.",
    caveat: "Best on the pooled average, which can hide losses on a search type. A reference arm can itself be the best arm.",
  },
];

// Plain-language metric definitions (from RESULTS.md). `dir` = which direction is better.
export const METRIC_LABELS = {
  mrr: { name: "MRR", dir: "up", help: "Mean Reciprocal Rank — how high up is the first correct result? #1 = 1.0, #2 = 0.5, #3 = 0.33… averaged over all searches. Higher is better." },
  recall_at_k: { name: "Recall@k", dir: "up", help: "Of all the tools that should match, what fraction showed up in the top k? Higher is better." },
  hit_at_1: { name: "Hit@1", dir: "up", help: "How often is the very first result correct? Higher is better." },
  hit_at_k: { name: "Hit@k", dir: "up", help: "How often does at least one correct result appear in the top k? Higher is better." },
  rt_ms_median: { name: "Speed (med)", dir: "down", help: "Median round-trip time in milliseconds (includes network + poll wait). Lower is better. Compare strategies on the median." },
  rt_ms_p95: { name: "Speed (p95)", dir: "down", help: "95th-percentile round-trip time in milliseconds — the slow tail. Lower is better." },
};

// Case-type labels for the golden set.
export const CASE_TYPE_LABELS = {
  "known-item": { name: "Known-item", help: "One defensible right answer (exact name, RRID, or known synonym). Trustworthy scores." },
  topical: { name: "Topical", help: "A broader query where several tools are relevant. Seeded but needs SME curation before its recall scores are fully trusted." },
};

export function strategyLabel(key) {
  return STRATEGY_LABELS[key] || { name: key, tag: key, blurb: "", bestFor: "" };
}

// Line icons for nf-tools resourceType values (the 9 types in the registry). Used only
// for the nf-tools index; other indexes have their own (un-iconified) categories.
const ICON = (inner) =>
  `<svg class="type-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;

const TYPE_PATHS = {
  "Cell Line": '<circle cx="12" cy="12" r="8"/><circle cx="13.5" cy="10.5" r="2.4"/>',
  "Antibody": '<path d="M12 21v-7"/><path d="M12 14 7.5 5.5"/><path d="m12 14 4.5-8.5"/>',
  "Animal Model": '<circle cx="10" cy="12.5" r="5.3"/><circle cx="5.6" cy="7" r="2.1"/><circle cx="14.4" cy="7" r="2.1"/><path d="M15 14.5c2.4 0 3.4 1.8 5.8 1.8"/>',
  "Genetic Reagent": '<path d="M8 3c0 5 8 5 8 9s-8 4-8 9"/><path d="M16 3c0 5-8 5-8 9s8 4 8 9"/><path d="M9 7h6M9 17h6"/>',
  "Patient-Derived Model": '<circle cx="12" cy="8" r="3.2"/><path d="M5.5 20c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5"/>',
  "Organoid Protocol": '<circle cx="9" cy="10" r="3"/><circle cx="15.5" cy="9" r="2.3"/><circle cx="13" cy="15" r="2.6"/>',
  "Biobank": '<path d="M4 9l8-5 8 5"/><path d="M5.5 9v9M18.5 9v9M9.5 9v9M14.5 9v9"/><path d="M3.5 20h17"/>',
  "Clinical Assessment Tool": '<rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9.5 4V3h5v1"/><path d="m9 11 1.4 1.4L13 10M9 16l1.4 1.4L13 15"/>',
  "Computational Tool": '<path d="M9 8l-4 4 4 4"/><path d="m15 8 4 4-4 4"/>',
};
const FALLBACK_PATH = '<path d="M4 13l7 7 9-9V4h-7z"/><circle cx="15.5" cy="8.5" r="1.3"/>';

export function toolTypeIcon(type) {
  return ICON(TYPE_PATHS[type] || FALLBACK_PATH);
}
