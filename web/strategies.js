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

// Build the `tuned` strategy from a table's fields.yaml `query:` block (embedded in the
// table's data JSON by build_site.py). Port of strategies.py's tuned_strategy() + the
// candidate.py compiler it delegates to — the subset those two produce, which is every shape
// the tuner can pick as a winner.
//
// Without this the lab would apply a tuned table's boosts to `multi_match_boosted` and label
// the result the table's config, which is a DIFFERENT query whenever the winner uses
// tie_breaker / minimum_should_match / phrase_boost. Boosts come from `fields`, never from
// the block, exactly as on the Python side.
export function makeTuned(spec) {
  const core = (q, fields) => {
    if (spec.query_type === "simple_query_string") {
      const body = { query: q, fields };
      if (spec.minimum_should_match) body.minimum_should_match = spec.minimum_should_match;
      return { simple_query_string: body };
    }
    const mmType = spec.multi_match_type || "best_fields";
    const body = { query: q, type: mmType, fields };
    // The live index rejects fuzziness outright for phrase types and cross_fields.
    if (!["phrase", "phrase_prefix", "cross_fields"].includes(mmType)
        && spec.fuzziness != null) body.fuzziness = spec.fuzziness;
    if (mmType === "best_fields" && spec.tie_breaker != null) body.tie_breaker = spec.tie_breaker;
    if (spec.minimum_should_match) body.minimum_should_match = spec.minimum_should_match;
    return { multi_match: body };
  };
  return (q, size, fields) => {
    const clause = core(q, fields);
    const pb = spec.phrase_boost;
    if (pb && pb.fields && pb.fields.length) {
      const phrase = { multi_match: { query: q, type: "phrase", fields: pb.fields,
                                      boost: pb.boost ?? 2 } };
      return { query: { bool: { must: [clause], should: [phrase] } }, size };
    }
    return { query: clause, size };
  };
}

// Stable display order (matches strategies.py STRATEGIES insertion order). Not const: the
// per-table `tuned` recipe is added and removed as indexes are loaded, and ES module live
// bindings mean importers see the reassignment.
export let STRATEGY_ORDER = Object.keys(STRATEGIES);

// Strategies that pass the (editable) per-field boosts through as-is, i.e. the ones the
// sidebar's "Custom Field Boosts" panel actually affects.
export const BOOSTED_KEYS = new Set(["multi_match_boosted", "multi_match_cross", "boosted_fuzzy", "phrase_prefix"]);

// Register the loaded table's `tuned` recipe, or clear it with a falsy `spec`. Called on every
// index load, so switching from a tuned table to an untuned one (or to a live-discovered index,
// which has no fields.yaml at all) removes the recipe rather than leaving a stale one that
// would be scored against the wrong table's config.
export function setTuned(spec) {
  delete STRATEGIES.tuned;
  BOOSTED_KEYS.delete("tuned");
  if (spec) {
    STRATEGIES.tuned = makeTuned(spec);
    BOOSTED_KEYS.add("tuned");   // it reads `fields`, so the boost editor drives it
  }
  STRATEGY_ORDER = Object.keys(STRATEGIES);
}
