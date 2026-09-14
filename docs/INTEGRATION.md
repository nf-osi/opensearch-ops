# Frontend integration reference

This describes how the Synapse web frontend (`synapse-web-monorepo`) currently integrates with and
queries the SearchIndex (OpenSearch) API. We want to maintain awareness of what the **platform default**
query is — the one a portal gets when it has not customized its search — where it's built in code, and how
it compares to the strategies we evaluate here. It is the control for our benchmarks, not what the NF
portal serves: NF ships its own recipe.

**Source:** `synapse-web-monorepo`, branch `origin/main` @ commit `a9f221ddb30`
(2026-06-18). SearchIndex integration in **PORTALS-4300** (`e0e94fa4a96`).
Line numbers below are anchored to that commit. Re-check and update this doc if `main` has moved.

## Status quo

The frontend queries the SearchIndex API, and the default full-text query is
a bare **`multi_match` with `fuzziness: "AUTO"`** — no field list, no boosts, no explicit
type.

```jsonc
// the entire default text query the frontend sends:
{ "multi_match": { "query": <user text>, "fuzziness": "AUTO" } }
```

Because no `fields` are specified, OpenSearch searches **all indexed fields with equal
weight**; because no `type` is set, it uses the `best_fields` default.

## Reference source

**`packages/synapse-react-client/src/components/SearchQueryWrapper/SearchQueryUseQueryOptions.ts`**
— function `toSearchIndexQuery(queryBundleRequest, searchIndexId)` (defined at **line 138**)
translates the portal's table-style query into a `SearchIndexQuery`.

| What | Where | Detail |
| --- | --- | --- |
| Only multi_match shape used | **line 51** | `type MultiMatchClause = { multi_match: { query: string; fuzziness: string } }` — no `fields`/`boost`/`type` |
| Text + facet filters | **line 222** | `bool.must: [{ multi_match: { query, fuzziness: 'AUTO' } }]`, `bool.filter: [...]` |
| Text only | **line 227** | `{ multi_match: { query, fuzziness: 'AUTO' } }` |
| Facet filters only (no text) | **line 229** | `{ bool: { filter: filterClauses } }` |
| No text, no filters | **line 231** | `{ match_all: {} }` |
| Page size / offset | **lines 247–248** | `size: query.limit`, `from: query.offset` |
| Sort | **lines ~236–245** | maps `query.sort` → `[{ column: 'asc'|'desc' }]`; default is relevance |

Free text comes from `TextMatchesQueryFilter.searchExpression` values, concatenated with
spaces (lines 204–215). Facet selections become `terms`/`range` filter clauses (non-scoring).

## NF portal specifically

- `apps/portals/nf/src/config/resources.ts:42` — `export const toolsSearchIndexId = 'syn75081636'` (our **nf-tools** index). (Lines 38–50 declare all 13 NF SearchIndex ids.)
- `apps/portals/nf/src/config/synapseConfigs/tools.tsx:85` — `searchIndexId: toolsSearchIndexId` passed into a `SearchQueryWrapperPlotNav` config.

So the NF tools page queries `nf-tools` through exactly the `toSearchIndexQuery` builder above.

## Control surfaces today (frontend layer vs configuration layer)

Tuning splits across two layers with different owners, edit points, and lifecycles. A key
distinction: a per-field **weight** is always *applied* at query time (the only place
OpenSearch still does per-field weighting), but synonyms/analyzers are *applied* at index
build time. That's why changing synonyms needs a reindex while changing a boost would not.

| Lever | Layer | Portal-owner controllable today? | How | Takes effect after |
| --- | --- | --- | --- | --- |
| Which SearchIndex a page queries | Frontend | ✅ yes | `searchIndexId` in `synapseConfigs` (e.g. `tools.tsx:85`) | redeploy |
| Facets / columns / cards shown | Frontend | ✅ yes | display props on `SearchQueryWrapperPlotNav` (`tableConfiguration`, `cardConfiguration`, `facetsToPlot`, `availableFacets`) | redeploy |
| Default page size / initial facet filters / sort | Frontend | ✅ yes | `initialLimit`, `initQueryRequest` (`selectedFacets`/`limit`/`offset`) | redeploy |
| Which fields full-text search covers | Frontend (query-time) | ❌ no | hard-coded to *all* fields — no `fields` in the `multi_match` (`SearchQueryUseQueryOptions.ts:51`) | SRC code change |
| Per-field boosts (`field^N`) | Frontend (query-time) | ❌ no | not built — `MultiMatchClause` has no `fields`/`boost` | SRC code change |
| Query type (`best_fields`/`phrase`/…) | Frontend (query-time) | ❌ no | hard-coded `best_fields` default | SRC code change |
| Fuzziness | Frontend (query-time) | ❌ no | hard-coded `AUTO` (`:51`, `:222`, `:227`) | SRC code change |
| Default tokenization analyzer | Configuration (index-time) | ⚠️ via REST API | `SearchConfiguration.defaultAnalyzer`, bound to the entity | **reindex** |
| Per-column analyzer (index + search) | Configuration (index-time) | ⚠️ via REST API | `ColumnAnalyzerOverride` entries in the `SearchConfiguration` | **reindex** |
| Synonyms | Configuration (index-time) | ⚠️ via REST API | `TextAnalyzer` (`$ref` a synonym set) referenced by the analyzer | **reindex** |
| Per-field weight via configuration | Configuration | ❌ no | not modeled on `SearchConfiguration` (analyzer-only schema) | n/a |

Legend: ✅ self-serve in portal config files · ⚠️ possible but only by creating/binding
objects through the Search Management REST API (needs org/entity permissions; no self-serve
UI) · ❌ not controllable without a `synapse-react-client` (SRC) code change.

**Net for portal owners today:** you control *which* index a page uses and *how results are
displayed/faceted* from your own `synapseConfigs` files. You do **not** control the full-text
matching strategy (fields, boosts, query type, fuzziness) — those are hard-coded in SRC — nor
field weighting on the configuration side (it isn't in the `SearchConfiguration` schema).
Analyzer and synonym tuning *is* available, but only via the REST API and only after a
reindex. For NF specifically, nothing is wired yet: the `org.synapse.nf` synonym sets exist
but aren't referenced by any `TextAnalyzer`/`SearchConfiguration`, and nothing is bound to
`syn74909065`, so every NF index runs on the system-default analyzer.

## Comparison to evaluated strategies

The platform default ≈ **`multi_match_best` over all fields + `fuzziness: AUTO`, with no field
boosts** — i.e. closest to our `boosted_fuzzy` strategy but *without* the boosts. Two
observations from [RESULTS.md](../benchmark/tools/RESULTS.md) (control, nf-tools):

1. **`fuzziness: AUTO` was the single biggest drag** in our test set: `boosted_fuzzy` was
   the worst strategy (MRR 0.833, Recall@10 0.778) because NF tool data is identifier-heavy
   (RRIDs like `CVCL_8478`, clone names like `ipNF95.11b`) where fuzzy matching introduces
   wrong hits. The plain non-fuzzy `multi_match_best` scored MRR 1.000 on the same set.
2. **No field boosts** means a description hit competes equally with a tool-name hit. Our
   hand-tuned boosts didn't help on this small set (they slightly *demoted* a synonym
   match), so this isn't a clear win either way yet — it needs the larger golden set.

**Tentative takeaway (needs a bigger golden set before acting):** the platform default's
`fuzziness: AUTO` looks suboptimal for this identifier-heavy index and is the first
thing worth A/B-ing for a portal still on the default. This is a frontend-side query parameter (`SearchQueryUseQueryOptions.ts`
line 51 / 222 / 227), so changing it is a frontend change, independent of the index-side
analyzer/synonym config.

Note the frontend currently has **no hook to vary fields, boosts, or query type** — the
`multi_match` shape is hard-coded. Tuning beyond fuzziness would require widening
`MultiMatchClause` and `toSearchIndexQuery`.
