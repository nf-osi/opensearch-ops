// Browser port of benchmark/strategies.py — query builders mapping
// (queryText, size, fields) -> a raw OpenSearch searchQuery DSL object.
//
// MUST stay in sync with benchmark/strategies.py. The standing guard is the benchmark
// parity check: the live scoreboard's per-strategy MRR/Recall/Hit must match
// `python3 benchmark/run.py tools` within rounding (see README / plan).
//
// `fields` is the table's match list with per-field boosts (["resourceName^5", ...]).
// The equal-weight strategies strip the `^weight` via unboosted(); boosted ones pass it
// through. All clauses use the verbose object form (Synapse's JSON adapter rejects shorthand).

// Drop the `^weight` so a strategy matches all fields equally.
function unboosted(fields) {
  return fields.map((f) => f.split("^", 1)[0]);
}

export const STRATEGIES = {
  // EXACT shape the live Synapse frontend sends today: a bare multi_match with
  // fuzziness AUTO, no field list (all fields, equal weight), no boosts. The production
  // baseline every other strategy is measured against; intentionally ignores `fields`.
  frontend_default: (q, size) => ({
    query: { multi_match: { query: q, fuzziness: "AUTO" } }, size,
  }),

  simple_query_string: (q, size, fields) => ({
    query: { simple_query_string: { query: q, fields: unboosted(fields) } }, size,
  }),

  multi_match_best: (q, size, fields) => ({
    query: { multi_match: { query: q, fields: unboosted(fields), type: "best_fields" } }, size,
  }),

  multi_match_boosted: (q, size, fields) => ({
    query: { multi_match: { query: q, fields, type: "best_fields" } }, size,
  }),

  multi_match_cross: (q, size, fields) => ({
    query: { multi_match: { query: q, fields, type: "cross_fields" } }, size,
  }),

  boosted_fuzzy: (q, size, fields) => ({
    query: { multi_match: { query: q, fields, type: "best_fields", fuzziness: "AUTO" } }, size,
  }),

  // typeahead-style: matches a leading prefix on the last token
  phrase_prefix: (q, size, fields) => ({
    query: { multi_match: { query: q, fields, type: "phrase_prefix" } }, size,
  }),
};

// Stable display order (matches strategies.py STRATEGIES insertion order).
export const STRATEGY_ORDER = Object.keys(STRATEGIES);
