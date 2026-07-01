// Auto-generate a field-boost configuration for an arbitrary SearchIndex from its
// columns (discovered live via SELECT_COLUMNS). Curated tables (e.g. nf-tools) ship a
// hand-tuned fields.yaml; every other index gets this heuristic instead, so the app can
// point at any portal's index without a config checked in.
//
// Heuristic: only free-text columns are searchable match targets; identifiers, dates,
// numbers, entity refs, and boilerplate are excluded. Boost tiers follow the same logic
// as the nf-tools fields.yaml (names strongest, then synonyms/ids, then discovery
// categoricals, then free text).

// Synapse columnTypes worth matching as text. Everything else (ENTITYID, DATE, INTEGER,
// DOUBLE, BOOLEAN, FILEHANDLEID, USERID, *_LIST of those, …) is dropped automatically.
const TEXTUAL = new Set(["STRING", "STRING_LIST", "LARGETEXT", "MEDIUMTEXT"]);

// Drop even-textual columns that are identifiers / links / boilerplate, not discovery text.
const SKIP_NAME = /(^id$|_id$|url|uri|email|orcid|synapseid|contact|acknowledg|requirement|disclaimer|howtoacquire|usage|biobankurl)/i;

const RE_NAME = /(^|_)(name|title|label)($|_)|name$|^name$|title$/i;     // identity → 5
const RE_ALIAS = /(synonym|alias|abbrev|acronym|rrid|symbol)/i;          // alt names / ids → 4
const RE_TOPIC = /(type|categor|status|focus|disease|tumor|phenotype|manifestation|species|organism|tissue|modality|initiative|agency|funder|kind|class|antigen|target|disorder|method|format)/i; // discovery → 2
const RE_FREE = /(description|summary|abstract|overview|notes?|detail|background|statement|text)/i; // free text → 1

function boostFor(name) {
  const n = name.toLowerCase();
  if (RE_NAME.test(n)) return 5;
  if (RE_ALIAS.test(n)) return 4;
  if (RE_TOPIC.test(n)) return 2;
  if (RE_FREE.test(n)) return 1;
  return 1;
}

// columns: [{ name, columnType }] from a SearchQueryResults.selectColumns.
// Returns ["field^N", …] (boost omitted when 1), highest boost first — the same shape
// run.py / strategies.js consume. Falls back to all-text-at-1, then ["*"].
export function generateFieldBoosts(columns) {
  const kept = [];
  for (const c of columns || []) {
    if (!TEXTUAL.has(c.columnType)) continue;
    if (SKIP_NAME.test(c.name)) continue;
    kept.push({ field: c.name, boost: boostFor(c.name) });
  }
  if (!kept.length) {
    const text = (columns || []).filter((c) => TEXTUAL.has(c.columnType)).map((c) => c.field || c.name);
    return text.length ? text : ["*"];
  }
  kept.sort((a, b) => b.boost - a.boost || a.field.localeCompare(b.field));
  return kept.map(({ field, boost }) => (boost === 1 ? field : `${field}^${boost}`));
}
