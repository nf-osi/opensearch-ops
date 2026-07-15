# opensearch-ops

Testing and benchmarking workspace for tuning Synapse's OpenSearch-backed search:
indexes, analyzers, synonyms, and query parameters. The goal is to iterate on the
NF search index definitions and search configuration, and measure their effect on
result quality.

## What an "index object" is

The index objects are Synapse entities of type **`SearchIndex`**
(`org.sagebionetworks.repo.model.search.table.SearchIndex`), stored as children of
project [`syn74909065`](https://www.synapse.org/Synapse:syn74909065)
(*"Portal Search Search Index Collections"*).

Each `SearchIndex` is defined by a **`definingSQL`** query (`SELECT ... FROM <synId>`)
against a source Synapse table/view. That query is the content that gets indexed into
OpenSearch. The project holds SearchIndex objects for multiple DCC portals
(adkp, ampals, ark, b2ai, cckp, challenges, classic, dh, elite, **nf**); the `nf-`
prefixed ones below are ours.

> [!NOTE]
> `SearchIndex` is a distinct value (`searchindex`) in the Synapse `EntityType` enum.
> `POST /repo/v1/entity/children` returns these objects only when `searchindex` is included
> in `includeTypes`.

## NF index objects (`nf-` prefixed)

Inventory as of 2026-06-19. All are children of `syn74909065`.

| SearchIndex id | name | definingSQL (source) |
| --- | --- | --- |
| `syn75081630` | `nf-datasets` | `SELECT * FROM syn50913342` |
| `syn75081641` | `nf-development-publications` | `SELECT * FROM syn51735467` |
| `syn75081638` | `nf-funders` | `SELECT * FROM syn16858699` |
| `syn75081639` | `nf-hackathons` | `SELECT * FROM syn25585549` |
| `syn75081635` | `nf-initiatives` | `SELECT * FROM syn24189696 ORDER BY initiative ASC` |
| `syn75081643` | `nf-mutations` | `SELECT externalMutationID, alleleType, mutationType, mutationMethod, affectedGeneSymbol, affectedGeneName, sequenceVariation, proteinVariation, animalModelMutation, humanClinVarMutation, chromosome FROM syn51750823` |
| `syn75081640` | `nf-observations` | `SELECT * FROM syn51735464` |
| `syn75081637` | `nf-people` | `SELECT * FROM syn23564971` |
| `syn75081631` | `nf-publications` | `SELECT * FROM syn16857542` |
| `syn75081644` | `nf-publications-v2` | `SELECT * FROM syn51735450` |
| `syn75081633` | `nf-studies` | `SELECT * FROM syn52694652` |
| `syn75081642` | `nf-tool-study` | `SELECT * FROM syn26461958` |
| `syn75081636` | `nf-tools` | `SELECT * FROM syn51730943` |

## Search Management API

`SearchIndex` objects and their tuning knobs are managed by **Search Management
Services** (base `https://repo-prod.prod.sagebase.org/repo/v1`). Full docs:
<https://rest-docs.synapse.org/rest/index.html#org.sagebionetworks.repo.web.controller.SearchManagementController>

The index objects themselves are managed as entities:

| Action | Endpoint |
| --- | --- |
| List index objects in a project | `POST /entity/children` with `includeTypes:["searchindex"]` |
| Get an index object | `GET /entity/{id}` |
| Run a search query (async) | `POST /search/query/async/start` → `GET /search/query/async/get/{asyncToken}` |
| Autocomplete | `POST /search/autocomplete` |
| Bind/get/unbind a search config to a Project/Folder | `PUT` / `GET` / `DELETE /entity/{entityId}/searchconfig/binding` |
| Bind a search config directly to a `SearchIndex` | set `searchConfigurationId` via `PUT /entity/{id}` (normal entity update — see below) |

### Where to bind a config (`SearchIndex`: set its own `searchConfigurationId`)

> [!WARNING]
> **Breaking API change (discovered 2026-07-07).** The generic binding endpoint used to
> accept `SearchIndex` as a target — the rest-docs said *"Attach a SearchConfiguration to an
> entity (SearchIndex, Folder, or Project) by creating a binding."* That's no longer true: the
> docs now say *"Bind a SearchConfiguration to an entity (**typically a project**)"*, and
> `PUT /entity/{entityId}/searchconfig/binding` on a `SearchIndex` now returns 400:
> `"A search configuration can only be bound to a Project or Folder."` This only blocks *new*
> bindings — `nf-tools`'s binding (created before the change) still resolves fine via
> `GET .../searchconfig/binding`; it just can no longer be created/recreated that way.

A `SearchConfiguration` is an **override** of the platform default analysis. Per the
`SearchIndex` [model docs](https://rest-docs.synapse.org/rest/org/sagebionetworks/repo/model/search/table/SearchIndex.html),
there are now two distinct ways it takes effect:

1. **Direct** — the `SearchIndex` entity's own `searchConfigurationId` field (`STRING`,
   optional). Set it with a normal entity update: `GET /entity/{id}`, add/update the field,
   `PUT /entity/{id}` it back. This **also fires a full index rebuild** as a side effect of
   the entity update — bind and rebuild happen in one call.
2. **Inherited** — if the field is unset, the build walks up the hierarchy (entity → folder →
   project) via `GET /entity/{id}/searchconfig/binding`, looking for the nearest Project/Folder
   binding created with the (now Project/Folder-only) generic binding endpoint. If none is
   found, platform defaults apply.

**Bind a table-specific config directly via its `SearchIndex` object's `searchConfigurationId`**
(e.g. the nf-tools config → `syn75081636`), **not** via a Project/Folder binding on the shared
collection project. The reason is unchanged: all portals' indexes are flat children of one
shared project [`syn74909065`](https://www.synapse.org/Synapse:syn74909065), so a Project-level
binding would resolve down to **every portal's** index, not just ours — the per-index
`searchConfigurationId` field is the only way to scope a config to one portal's index. (Note the
index object and its `definingSQL` source table live in *different* project trees — the source
table's project, e.g. NF's `syn26338068`, is **not** an ancestor of the index object, so a
Project/Folder binding there would not reach the index either way.)

> [!IMPORTANT]
> **Setting `searchConfigurationId` already triggers a rebuild** — it's a normal entity
> update, and any `SearchIndex` entity update (create/update/delete) fires the same rebuild
> lifecycle: delete + recreate the OpenSearch index with the currently-effective config, then
> re-stream all rows. A *separate* touch (`PUT /entity/{id}` with no field changes) is only
> needed to force a rebuild **without** changing which config is bound — e.g. after editing a
> referenced analyzer/override object in place.
>
> Constraints: creating/updating a `SearchIndex` entity is restricted to Sage
> employees/admins; indexing runs anonymously, so only public **OPEN_DATA** rows are indexed.
> Query-time changes (DSL boosts, fuzziness, the search-analyzer half of an override) need no
> rebuild.

> [!NOTE]
> **Current state:** six NF indexes are registered and bound — `nf-tools` (config id `9`,
> inherited via its pre-change binding), `nf-datasets` (`11`), `nf-hackathons` (`12`),
> `nf-initiatives` (`13`), `nf-studies` (`10`), `nf-publications` (`14`) — all via the direct
> `searchConfigurationId` field. Check any entity's binding with [`config/config.py check`](config/config.py),
> list all registered configs with [`config/config.py list`](config/config.py), and bind/rebuild
> with [`config/config.py apply <id> --index <name-or-synId>`](config/config.py).

The tuning objects (organization-scoped; list with optional `organizationName` filter):

| Object type | List | Get / Update |
| --- | --- | --- |
| Text analyzer | `POST /search/text/analyzer/list` | `GET` / `PUT /search/text/analyzer/{id}` |
| Column analyzer override | `POST /search/column/analyzer/override/list` | `GET` / `PUT /search/column/analyzer/override/{id}` |
| Synonym set | `POST /search/synonym/set/list` | `GET` / `PUT /search/synonym/set/{synonymSetId}` |
| Search configuration | `POST /search/configuration/list` | `GET` / `PUT /search/configuration/{searchConfigurationId}` |

### NF tuning objects (org `org.synapse.nf`)

| Type | id | name | notes |
| --- | --- | --- | --- |
| Synonym set | `16` | `standard_synonyms` | `synonym_graph`; `nf→neurofibromatosis`, `mpnst→…`, `pnf→plexiform neurofibroma`, etc. |
| Synonym set | `17` | `synonym_rules` | `synonym_graph`, empty (placeholder) |
| Text analyzer | `1017` | `nf_scientific_synonyms` | standard+lowercase+english stop/stemmer; `default_search` adds set 16 via `synonym_graph` (search-time only) |
| Column analyzer override | `9` | `nf_tools_columns` | per-column map for nf-tools: IDENTIFIER for id fields, `nf_scientific_synonyms` for discovery free-text, KEYWORD for clean categoricals |
| Search configuration | `9` | `nf_tools_search_config` | `defaultAnalyzer`=STANDARD + `columnAnalyzerOverrides`=[`nf_tools_columns`]; **bound to `syn75081636`** |

The other indices follow the same pattern — their own `ColumnAnalyzerOverride` +
`SearchConfiguration` pair (reusing the shared `nf_scientific_synonyms` text analyzer), bound to
their own `SearchIndex`: `nf-datasets`, `nf-hackathons`, `nf-initiatives`, `nf-studies`,
`nf-publications`. See [`config/config.py list`](config/config.py) for the full current registry
rather than duplicating it here.

These NF objects are versioned in [`config/`](config/) and (re)created/updated with
[`config/config.py register`](config/config.py); the resulting SearchConfiguration id is
then bound to a SearchIndex with [`config/config.py apply <id>`](config/config.py). They
reference the platform's shared
built-in text analyzers (org `org.sagebionetworks`), available to any config:
`SCIENTIFIC` (1), `STANDARD` (2), `IDENTIFIER` (3), `KEYWORD` (4), `AUTOCOMPLETE` (5).

The custom `nf_scientific_synonyms` analyzer deliberately omits `word_delimiter_graph`:
OpenSearch parses each synonym definition through the filters *preceding* the
`synonym_graph` filter and rejects graph filters (like `word_delimiter_graph`) there, so
only `lowercase` precedes the synonyms. Identifier-style token splitting is instead handled
by the `IDENTIFIER` analyzer on the id columns.

## Querying an index (focus: `nf-tools`)

> [!IMPORTANT]
> **The deployed API differs from the public rest-docs.** The rest-docs OpenAPI describes a
> *structured* `SearchQuery` (`queryType`, `queryFields`, `termsFilters`, `fuzziness`,
> `limit`, …), but `repo-prod` no longer runs that model — it accepts a **raw OpenSearch
> query DSL** object in `searchQuery` and rejects the structured fields (`"JSON Element ...
> Unsupported: queryType/limit/queryText"`). This gives the client the full power of
> OpenSearch; benchmark against the raw DSL.

**No authentication is required; these indexes are public and queries work anonymously.**

Use [`query.py`](query.py) (handles polling and field flattening):
```bash
python3 query.py '{"query":{"multi_match":{"query":"schwann","fields":["resourceName^3","description","synonyms"]}},"size":5}'
```

### More details

Query flow (async job pattern):
1. `POST /search/query/async/start` with a `SearchIndexQuery` → returns `{token}`.
2. `GET /search/query/async/get/{token}` → while still running it returns HTTP 200 with
   `jobState: "PROCESSING"` (keep polling); the final payload omits `jobState` and
   contains `hits`, `totalHits`, `selectColumns`.

Request body:
```json
{
  "concreteType": "org.sagebionetworks.repo.model.search.table.SearchIndexQuery",
  "searchIndexId": "syn75081636",
  "responseParts": ["HITS", "TOTAL_HITS", "SELECT_COLUMNS"],
  "searchQuery": { "query": { "match_all": {} }, "size": 3 }
}
```
`searchQuery` is raw OpenSearch DSL — `query`, `size`, `from`, `sort`, `highlight`, `aggs`, etc.
See the OpenSearch [Query DSL](https://docs.opensearch.org/latest/query-dsl/) docs, and in
particular [`multi_match` query types](https://docs.opensearch.org/latest/query-dsl/full-text/multi-match/)
(`best_fields`, `cross_fields`, `phrase`, `phrase_prefix`, …) — the `type` the benchmark
[strategies](benchmark/strategies.py) vary.

> [!WARNING]
> Synapse's JSON adapter rejects OpenSearch shorthand. Use the verbose object form for every
> clause — `{"match":{"description":{"query":"plexiform"}}}`, **not**
> `{"match":{"description":"plexiform"}}` (the latter errors with
> `JSONObject["description"] is not a JSONObject`).

### `nf-tools` index (syn75081636)

- Source: `SELECT * FROM syn51730943` (NF Research Tools Central registry)
- ~1,216 documents, 46 indexed columns:

  `resourceId, rrid, resourceName, synonyms, description, resourceType,
  investigatorName, institution, investigatorSynapseId, orcid, usageRequirements,
  howToAcquire, species, cellLineCategory, cellLineGeneticDisorder,
  cellLineManifestation, backgroundStrain, backgroundSubstrain,
  animalModelGeneticDisorder, animalModelOfManifestation, insertName, insertSpecies,
  vectorType, targetAntigen, reactiveSpecies, hostOrganism, biobankName, biobankURL,
  specimenTissueType, specimenPreparationMethod, diseaseType, tumorType,
  specimenFormat, specimenType, contact, race, sex, age, dateAdded, dateModified,
  latestPublicationDate, completenessCategory, availabilityCategory,
  criticalInfoCategory, otherInfoCategory, observationCategory`

  Each hit returns `rowId`, `rowVersion`, `score`, and `fields` (column name/value
  pairs; multi-value columns are JSON-encoded strings).

## Benchmark harness

[`benchmark/`](benchmark/) compares query strategies against a golden (ground) relevance set so
we can measure the effect of config and query changes.

**Per-table layout.** Each table's benchmark lives in its own subfolder
`benchmark/<table>/`, e.g. [`benchmark/tools/`](benchmark/tools/) and
[`benchmark/studies/`](benchmark/studies/), …, each with its own goldens, docs, and results. The
shared harness scripts live at `benchmark/` root.

Shared harness (`benchmark/` root):
- [`benchmark/strategies.py`](benchmark/strategies.py) — query builders (`query_text → DSL`):
  `frontend_default` (mirrors the live frontend exactly — bare `multi_match` + `fuzziness:
  AUTO`, the production baseline), `simple_query_string`, `multi_match_best`,
  `multi_match_boosted`, `multi_match_cross`, `boosted_fuzzy`, `phrase_prefix`. The functions
  are table-agnostic; the field-boost lists
  default to nf-tools. A table whose schema differs can override by adding its own
  `benchmark/<table>/strategies.py` (run.py prefers it, else falls back here).
- [`benchmark/run.py`](benchmark/run.py) — runs every (case × strategy) for a table, scores
  **MRR**, **Recall@k**, **Hit@1**, **Hit@k**; prints a table and writes
  `benchmark/<table>/results/<label>.json` (`--label` defaults to `latest`).

```bash
pip install pyyaml   # one-time; run.py reads the golden set from YAML
python3 benchmark/run.py tools                                                   # -> results/latest.json
python3 benchmark/run.py tools --label boost-v2 --strategy multi_match_boosted   # after editing benchmark/strategies.py
```

**What the live frontend actually has** (a bare `multi_match` + `fuzziness: AUTO`, no
field boosts) is documented with code line references in
[docs/INTEGRATION.md](docs/INTEGRATION.md).

## Interactive app (Synapse Portal Search Lab)

[`web/`](web/) is a friendly **self-serve** version of the harness for business owners / SMEs that's a
static, dependency-free browser app (no build tooling, no backend). It calls the public
Synapse search API directly and has two modes:

- **Search playground** (entry-level) — type a query and compare two ranking *recipes* (strategies)
  side by side, with per-field boost sliders. Intuitive way to show how a tuning change reorders real
  results.
- **Benchmark scoreboard** (advanced) — reference a golden set × selected recipes live in the browser, 
  get same MRR / Recall / Hit table as `run.py`, with per-case drill-in.

**Works against any SearchIndex, any portal.** while nf-tools is the default, the index
picker is **populated by listing every SearchIndex in the collection project**
(`syn74909065`) via `entity/children` — any portal's index is one click away. When a
non-curated index is chosen, the app discovers its columns live (`SELECT_COLUMNS`) and
**auto-generates a field-boost config** from column type + name heuristics
([`web/boostgen.js`](web/boostgen.js)).
Curated tables (those with a `golden.yaml`) ship hand-tuned boosts and enable the
scoreboard; any other index runs in playground-only mode (no golden set → no benchmark).
The query recipes are identical for every index.

This is a visual **complement** to `benchmark/run.py`, not a replacement: `run.py` is the
engineer/CI path, the site is the non-engineer path. The recipes and scoring are JS ports
of [`strategies.py`](benchmark/strategies.py) and [`run.py`](benchmark/run.py); the golden
cases and field boosts are **generated** from the same YAML at build time (so they never
drift). The standing drift guard for the ported *logic* is a parity check: the live
scoreboard's per-strategy MRR/Recall/Hit must match `python3 benchmark/run.py tools` within
rounding.

> [!NOTE]
> Workbench optimizes **query-time** levers only (recipe, field boosts, fuzziness) and
> reflects the index's *current* production config. Index-time config (analyzers,
> synonyms in [`config/`](config/)) needs a Sage-admin index rebuild and isn't adjustable
> client-side.

### Usage

[`build_site.py`](build_site.py) assembles the site into `site/` (gitignored) — it copies
`web/` and emits `site/data/<table>.json` (golden cases + boosts + an optional precomputed
baseline scoreboard from `results/`). CI ([`.github/workflows/benchmark.yml`](.github/workflows/benchmark.yml))
runs `run.py` then `build_site.py` and publishes `site/` to GitHub Pages.

```bash
pip install pyyaml
python3 benchmark/run.py tools --label baseline   # optional: precomputed fast-default scoreboard
python3 build_site.py                              # -> site/
python3 -m http.server -d site                     # open http://localhost:8000
```

## Rechecking SearchIndex object inventory

No auth token needed — these objects are PUBLIC, so `entity/children` and
`entity/{id}` both work anonymously.

```bash
B="https://repo-prod.prod.sagebase.org/repo/v1"

# List all SearchIndex objects in the project (filter for nf- in name)
curl -s -X POST "$B/entity/children" \
  -H "Content-Type: application/json" \
  -d '{"parentId":"syn74909065","includeTypes":["searchindex"]}'

# Inspect one index object's definingSQL
curl -s "$B/entity/syn75081636"
```
