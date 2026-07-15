// Audience-facing copy for business owners / SMEs. Strategy and metric prose is lifted
// from benchmark/tools/RESULTS.md (the stakeholder-facing summary) so the site and the
// engineer-facing doc say the same thing. Keep names/wording in sync with that doc.

// Friendly name + one-line "what it does" + "best for" for each strategy key.
// `tag` is the short chip shown in compact UI.
export const STRATEGY_LABELS = {
  frontend_default: {
    name: "Default site search",
    tag: "live",
    blurb: "The front-end's built-in default query: searches every field, equal weight, with typo tolerance. No boosts.",
    bestFor: "The baseline — what users get right now.",
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
