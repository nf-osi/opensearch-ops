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

// ---------------------------------------------------------------- pre-strategy routing
// buildQueryClause (SearchQueryUseQueryOptions.ts:146) intercepts two kinds of query text
// ahead of the queryStrategy switch and routes BOTH to simple_query_string, keeping the
// portal's `fields` if it has any: a double-quoted phrase (multi_match cannot honour the
// phrase operator) and a Synapse entity id (fuzziness AUTO matches neighbouring ids).
// Applies whether or not the portal customized its search. Ports of containsQuotedPhrase()
// and SYNAPSE_ENTITY_ID_TOKEN_REGEX (RegularExpressions.ts:37).
const QUOTED_PHRASE_RE = /"[^"]+"/u;
const SYNAPSE_ID_RE = /\bsyn\d+(?:\.\d+)?\b/iu;

export function routesToSimpleQueryString(q) {
  return QUOTED_PHRASE_RE.test(q) || SYNAPSE_ID_RE.test(q);
}

export const STRATEGIES = {
  // The Synapse front-end's PLATFORM default: the query a portal gets when it has not set a
  // SearchQueryConfig — a bare multi_match with fuzziness AUTO, no field list (all fields,
  // equal weight), no boosts — except for quoted / Synapse-id queries, which route to a
  // bare simple_query_string. The control for the 12 NF indexes that ship no config; NOT
  // for nf-tools, which does (see makeProductionCurrent). Ignores `fields`.
  frontend_default: (q, size) => (
    routesToSimpleQueryString(q)
      ? { query: { simple_query_string: { query: q } }, size }
      : { query: { multi_match: { query: q, fuzziness: "AUTO" } }, size }
  ),

  simple_query_string: (q, size, fields) => ({
    query: { simple_query_string: { query: q, fields: unboosted(fields) } }, size,
  }),

  // What the routing above emits for a portal that sets fieldBoosts: a simple_query_string
  // that KEEPS the boosts. No other entry here has this shape.
  simple_query_string_boosted: (q, size, fields) => ({
    query: { simple_query_string: { query: q, fields } }, size,
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

// queryStrategy value -> the clause it emits, given the portal's boosted field list.
// Ports the switch in buildQueryClause. MULTI_MATCH is the one case that ignores
// fieldBoosts, which is why frontend_default sends no field list.
const QUERY_STRATEGY_CLAUSE = {
  MULTI_MATCH: (q, fields, fuzz) => ({ multi_match: { query: q, fuzziness: fuzz || "AUTO" } }),
  MULTI_MATCH_BEST_FIELDS: (q, fields, fuzz) => ({
    multi_match: { query: q, type: "best_fields", fields, ...(fuzz ? { fuzziness: fuzz } : {}) },
  }),
  MULTI_MATCH_CROSS_FIELDS: (q, fields, fuzz) => ({
    multi_match: { query: q, type: "cross_fields", fields, ...(fuzz ? { fuzziness: fuzz } : {}) },
  }),
  SIMPLE_QUERY_STRING: (q, fields) => ({ simple_query_string: { query: q, fields } }),
  PHRASE_PREFIX: (q, fields) => ({ multi_match: { query: q, type: "phrase_prefix", fields } }),
  BOOSTED_FUZZY: (q, fields, fuzz) => ({
    multi_match: { query: q, fields, fuzziness: fuzz || "AUTO" },
  }),
};

/** Compile a table's `production:` block (fields.yaml, carried in the table payload) into a
 *  strategy: what that portal page actually sends today, routing included. Pins the block's
 *  own fields, so editing the sidebar boosts does not move the control. Returns null when
 *  the table ships no SearchQueryConfig — its control is frontend_default.
 *  Mirrors compile_production() in benchmark/strategies.py. */
export function makeProductionCurrent(spec) {
  if (!spec) return null;
  const clause = QUERY_STRATEGY_CLAUSE[spec.query_strategy || "MULTI_MATCH"];
  if (!clause) throw new Error(`unknown query_strategy ${spec.query_strategy}`);
  const prodFields = spec.fields?.length ? spec.fields : undefined;
  return (q, size) => {
    if (routesToSimpleQueryString(q)) {
      return {
        query: { simple_query_string: { query: q, ...(prodFields ? { fields: prodFields } : {}) } },
        size,
      };
    }
    return { query: clause(q, prodFields, spec.fuzziness), size };
  };
}

// Stable display order (matches strategies.py STRATEGIES insertion order).
export const STRATEGY_ORDER = Object.keys(STRATEGIES);

// Strategies that pass the (editable) per-field boosts through as-is, i.e. the ones the
// sidebar's "Custom Field Boosts" panel actually affects. production_current is NOT here:
// it pins production's own boosts on purpose.
export const BOOSTED_KEYS = new Set(["simple_query_string_boosted", "multi_match_boosted",
                                     "multi_match_cross", "boosted_fuzzy", "phrase_prefix"]);
